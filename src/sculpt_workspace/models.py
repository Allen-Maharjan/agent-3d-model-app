"""Typed request models shared by the HTTP API and the MCP tools."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Vec3 = tuple[float, float, float]
Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

NAMED_VIEWS = ("front", "right", "back", "left", "top", "perspective")
STANDARD_VIEWS = ("front", "right", "back", "perspective")
NamedView = Literal["front", "right", "back", "left", "top", "perspective"]
# Extra render passes next to the colour image; see blender_scripts/render_views.py.
ExtraPass = Literal["highlight", "normal", "depth"]
PassName = Literal["color", "highlight", "normal", "depth"]
PASS_SEPARATOR = "--"  # noqa: S105 - not a secret
CLOSEUP_PREFIX = "closeup_"

MAX_QUERY_RESULTS = 1000
MAX_SOURCE_BYTES = 1_000_000
MAX_BRIEF_CHARS = 4000
MAX_WAIT_SECONDS = 50.0  # stays under the common 60 s MCP client tool timeout (e.g. Codex)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _finite(v: Vec3) -> Vec3:
    if not all(math.isfinite(c) for c in v):
        raise ValueError("vector components must be finite numbers")
    return v


def _norm(v: Vec3) -> float:
    return math.sqrt(sum(c * c for c in v))


class ViewFrame(Strict):
    """Viewing frame: `front` points from the sculpture toward the front viewer.

    Blender's default is Z-up with the front viewed from -Y, i.e.
    front = (0, -1, 0), up = (0, 0, 1). Vectors are normalised on validation and
    must be perpendicular within 0.5 degrees.
    """

    front: Vec3 = (0.0, -1.0, 0.0)
    up: Vec3 = (0.0, 0.0, 1.0)

    @model_validator(mode="after")
    def _check(self) -> ViewFrame:
        f, u = _finite(self.front), _finite(self.up)
        nf, nu = _norm(f), _norm(u)
        if nf < 1e-9 or nu < 1e-9:
            raise ValueError("front and up must be non-zero vectors")
        dot = sum(a * b for a, b in zip(f, u, strict=True)) / (nf * nu)
        if abs(dot) > math.sin(math.radians(0.5)):
            raise ValueError("front and up must be perpendicular")
        object.__setattr__(self, "front", tuple(c / nf for c in f))
        object.__setattr__(self, "up", tuple(c / nu for c in u))
        return self


class CameraSpec(Strict):
    name: Identifier
    location: Vec3
    target: Vec3
    up: Vec3 | None = None
    type: Literal["perspective", "orthographic"] = "perspective"
    lens_mm: float = Field(default=50.0, gt=1.0, le=1000.0)
    ortho_scale: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _check(self) -> CameraSpec:
        _finite(self.location)
        _finite(self.target)
        if _norm(tuple(a - b for a, b in zip(self.location, self.target, strict=True))) < 1e-9:  # type: ignore[arg-type]
            raise ValueError("camera location and target must differ")
        if self.up is not None and _norm(_finite(self.up)) < 1e-9:
            raise ValueError("camera up must be non-zero")
        if PASS_SEPARATOR in self.name or self.name.startswith(CLOSEUP_PREFIX):
            raise ValueError(f"camera names must not contain {PASS_SEPARATOR!r} or start with {CLOSEUP_PREFIX!r}")
        return self


class RegionQuery(Strict):
    type: Literal["region"]
    min: Vec3
    max: Vec3
    object_ids: list[Identifier] | None = None
    limit: int = Field(default=200, ge=1, le=MAX_QUERY_RESULTS)

    @model_validator(mode="after")
    def _check(self) -> RegionQuery:
        if any(a > b for a, b in zip(_finite(self.min), _finite(self.max), strict=True)):
            raise ValueError("region min must be <= max on every axis")
        return self


class NearestQuery(Strict):
    type: Literal["nearest"]
    point: Vec3
    max_distance: float | None = Field(default=None, gt=0.0)
    object_ids: list[Identifier] | None = None

    @field_validator("point")
    @classmethod
    def _check_point(cls, v: Vec3) -> Vec3:
        return _finite(v)


class SamplesQuery(Strict):
    type: Literal["surface_samples"]
    count: int = Field(default=100, ge=1, le=MAX_QUERY_RESULTS)
    seed: int = 0
    object_ids: list[Identifier] | None = None


class RayQuery(Strict):
    type: Literal["ray"]
    origin: Vec3
    direction: Vec3
    max_distance: float | None = Field(default=None, gt=0.0)
    object_ids: list[Identifier] | None = None

    @model_validator(mode="after")
    def _check(self) -> RayQuery:
        _finite(self.origin)
        if _norm(_finite(self.direction)) < 1e-12:
            raise ValueError("ray direction must be non-zero")
        return self


class ImageRayQuery(Strict):
    """Ray through a rendered image pixel.

    `u`, `v` are normalised image coordinates with the origin at the TOP-LEFT
    corner: u grows to the right, v grows downward, both in [0, 1].
    """

    type: Literal["image_ray"]
    artifact_id: Identifier
    u: float = Field(ge=0.0, le=1.0)
    v: float = Field(ge=0.0, le=1.0)
    object_ids: list[Identifier] | None = None


class MeasureQuery(Strict):
    """Size of an object and how it sits relative to other geometry.

    `target_ids` defaults to the original (protected) objects. Reports the
    object's bounds and size (also in mm), the closest distance to the
    targets with the closest point pair, and how many face pairs intersect.
    """

    type: Literal["measure"]
    object_id: Identifier
    target_ids: list[Identifier] | None = Field(default=None, min_length=1, max_length=50)


SpatialQuery = Annotated[
    RegionQuery | NearestQuery | SamplesQuery | RayQuery | ImageRayQuery | MeasureQuery,
    Field(discriminator="type"),
]


class InspectRequest(Strict):
    """`revision_id` defaults to the project's current revision."""

    project_id: Identifier
    revision_id: Identifier | None = None
    query: SpatialQuery | None = None
    include_references: bool = False


class EditRequest(Strict):
    """An agent edit.

    `dry_run` validates and renders the candidate without publishing a
    revision (the base may be any revision). `strict_checks` rejects the edit
    when a geometry check on a changed object reports a warning; errors
    always reject. `from_dry_run` publishes the script of a succeeded dry run
    instead of `source` (and its summary unless one is given); the dry run's
    base must be `base_revision_id`.
    """

    project_id: Identifier
    base_revision_id: Identifier
    source: str | None = Field(default=None, min_length=1, max_length=MAX_SOURCE_BYTES)
    summary: str | None = Field(default=None, min_length=1, max_length=500)
    dry_run: bool = False
    strict_checks: bool = False
    from_dry_run: Identifier | None = None

    @model_validator(mode="after")
    def _check(self) -> EditRequest:
        if self.from_dry_run is not None:
            if self.source is not None:
                raise ValueError("pass either source or from_dry_run, not both")
            if self.dry_run:
                raise ValueError("from_dry_run publishes a dry run; it cannot itself be a dry run")
        elif self.source is None or self.summary is None:
            raise ValueError("source and summary are required unless from_dry_run is given")
        return self


class RenderRequest(Strict):
    project_id: Identifier
    revision_id: Identifier | None = None
    views: list[NamedView] | None = Field(default=None, max_length=6)
    cameras: list[CameraSpec] | None = Field(default=None, max_length=8)
    engine: Literal["workbench", "eevee"] = "workbench"
    resolution: int = Field(default=1024, ge=64, le=2048)
    frame: ViewFrame | None = None
    persist_frame: bool = False
    passes: list[ExtraPass] | None = Field(default=None, max_length=3)
    focus_object_ids: list[Identifier] | None = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def _check(self) -> RenderRequest:
        if self.persist_frame and self.frame is None:
            raise ValueError("persist_frame requires a frame")
        if self.focus_object_ids and self.cameras:
            raise ValueError("focus_object_ids frames named views; it cannot be combined with cameras")
        names = list(self.views or []) + [c.name for c in self.cameras or []]
        if len(set(names)) != len(names):
            raise ValueError("view and camera names must be unique")
        return self


class CheckoutRequest(Strict):
    project_id: Identifier
    revision_id: Identifier
    expected_current_revision_id: Identifier


class ExportRequest(Strict):
    project_id: Identifier
    revision_id: Identifier
    format: Literal["blend", "stl"]
    stl_units: Literal["mm", "m", "scene"] = "mm"


class Crop(Strict):
    """Normalised crop rectangle, top-left origin, in the full-resolution image."""

    view: Identifier
    x: float = Field(ge=0.0, lt=1.0)
    y: float = Field(ge=0.0, lt=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> Crop:
        if self.x + self.width > 1.0 + 1e-9 or self.y + self.height > 1.0 + 1e-9:
            raise ValueError("crop must lie inside the image")
        return self


def _color_only() -> list[PassName]:
    return ["color"]


class ResultRequest(Strict):
    job_id: Identifier
    include_images: bool = True
    views: list[Identifier] | None = Field(default=None, max_length=8)
    max_image_size: int = Field(default=512, ge=64, le=2048)
    crop: Crop | None = None
    passes: list[PassName] = Field(default_factory=_color_only, min_length=1, max_length=4)
    overlays: bool = False
    compare: bool = False
    wait_seconds: float = Field(default=0.0, ge=0.0, le=MAX_WAIT_SECONDS)
    detail: Literal["compact", "full"] = "compact"
    include_source: bool = False


class BriefRequest(Strict):
    project_id: Identifier
    brief: str = Field(max_length=MAX_BRIEF_CHARS)
