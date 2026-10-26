"""MCP adapter: tools and a playbook prompt over the same application services as the browser."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from importlib import resources
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ContentBlock, ImageContent, TextContent
from pydantic import Field, ValidationError

from .errors import InvalidRequest, SculptError
from .models import (
    CameraSpec,
    CheckoutRequest,
    Crop,
    EditRequest,
    ExportRequest,
    ExtraPass,
    InspectRequest,
    NamedView,
    PassName,
    RenderRequest,
    ResultRequest,
    SpatialQuery,
    ViewFrame,
)
from .services import Workspace

INSTRUCTIONS = """\
Sculpt Workspace lets you add to a sculpture in Blender: inspect -> edit -> render -> review.
Geometry from the original upload is protected and read-only; objects you add are yours.

Workflow
0. get_project(project_id) first: current revision id, revision history, latest render job id, brief
   and active jobs. Without project_id it lists projects. get_playbook() returns the full playbook.
1. inspect_project(project_id) returns the brief, reference images (include_references=true to see
   them), units, bounds and objects with ids (revision_id defaults to the current revision).
   Add a spatial `query` (region, nearest, surface_samples, ray, image_ray, measure) for precise
   targeting; `measure` reports an object's size in mm and its gap to / overlap with the sculpture.
2. apply_edit(project_id, base_revision_id, source, summary) runs Python in Blender with a `sculpt`
   helper (surface rays, placement, curves, booleans, shrinkwrap, materials, mm/cm units).
   dry_run=true validates and renders without publishing; publish a good one with from_dry_run.
   strict_checks=true rejects geometry warnings.
3. get_result(job_id, wait_seconds=45) waits for the job (and an edit's render) and returns images.
   For review use passes=["color","highlight"] (orange = changed in this edit) and compare=true
   (before | after); overlays=true adds a grid, scale bar and axis gizmo. Edit renders include
   close-ups of changes. include_source=true returns an edit's script.
4. render_views for other angles, focus_object_ids close-ups or normal/depth passes;
   checkout_revision to step back; export_model for .blend or STL.

Never guess coordinates from a picture: use image_ray, or sculpt.ray / sculpt.nearest in the script.
Work in small edits and review each one. Rejections include hints on how to fix them.

Conventions: Blender Z-up world, front viewed from -Y. Image probes use normalised (u, v) with a
TOP-LEFT origin. Every response is scoped to an explicit revision id.
"""

EDIT_DESCRIPTION = """\
Run a Python edit script against `base_revision_id` and publish it as a new revision after validation.
The base must be the project's current revision, except for dry runs. The script runs inside Blender
with `bpy`, `bmesh`, `mathutils`, `Vector`, `Matrix`, `math` and a `sculpt` helper:
  sculpt.objects() -> {object_id: Object}; sculpt.get(id); sculpt.id_of(obj)
  sculpt.protected_ids; sculpt.is_protected(obj_or_id); sculpt.link(obj, collection=None)
  sculpt.scene_bounds() -> (min, max) Vectors of the original sculpture
  sculpt.mm(x), sculpt.cm(x) -> Blender units; sculpt.to_mm(units_or_vector); sculpt.scale_length
  sculpt.ray(origin, direction, object_ids=None, max_distance=None) -> hit dict or None
  sculpt.nearest(point, object_ids=None, max_distance=None) -> hit dict or None
    (hit: position, normal, distance, object_id, object_name, face_index; default target: original)
  sculpt.place_on_surface(obj, position, normal, offset=0.0, axis="Z", up="Y", spin=0.0)
  sculpt.duplicate(obj, linked=False, name=None) -> unprotected copy (copies mesh data)
  sculpt.array_along(obj, positions, normals=None, offset=0.0, linked=False) -> [copies]
  sculpt.from_curve(points, radius=0.01, name="Curve", cyclic=False, smooth=True) -> mesh tube
  sculpt.conform_to(obj, target_id, offset=0.0, method="NEAREST_SURFACEPOINT", apply=True)
  sculpt.boolean(obj, cutter, operation="UNION", apply=True, remove_cutter=True)
  sculpt.material(name, color, metallic=0.0, roughness=0.5, emission=None); sculpt.assign_material(obj, mat)
  sculpt.apply_modifiers(obj) -> object to use from now on (curves become meshes)
Helpers refuse to change protected objects. Avoid bpy.ops: background Blender has no UI context.
A fresh Blender process then compares every protected object with the original upload; any change
rejects the edit with object ids, reasons and fix hints. Geometry checks run on objects this edit
added or modified (empty, non-finite, too large/small, non-manifold, inverted normals, floating,
intersecting the original): errors reject; warnings reject only with strict_checks=true.
dry_run=true validates, checks and renders the candidate without creating a revision; get_result on
the returned edit_job_id shows its images. To publish a succeeded dry run, pass from_dry_run=<its
edit_job_id> instead of source (summary optional): the same script is re-run and validated against the
same base, which must still be current. Otherwise returns edit_job_id and render_job_id; the render
highlights changes and adds close-ups. Use get_result(edit_job_id, wait_seconds=45). print() output
is returned.
"""


def _content(payload: dict[str, Any], images: list[tuple[bytes, dict[str, Any]]] | None = None) -> CallToolResult:
    blocks: list[ContentBlock] = [TextContent(type="text", text=json.dumps(payload, indent=1, default=str))]
    for data, meta in images or []:
        blocks.append(TextContent(type="text", text=f"Image: {json.dumps(meta)}"))
        blocks.append(ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png"))
    return CallToolResult(content=blocks, structured_content=payload)


def _error(exc: SculptError) -> CallToolResult:
    payload = {"error": exc.to_dict()}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=1, default=str))],
        structured_content=payload,
        is_error=True,
    )


async def _guard(fn: Callable[[], Any]) -> CallToolResult:
    try:
        return await fn()
    except ValidationError as exc:
        details = [{"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg")} for e in exc.errors()]
        return _error(InvalidRequest("Invalid arguments", details={"errors": details}))
    except SculptError as exc:
        return _error(exc)


def build_mcp(get_ws: Callable[[], Workspace], base_url: str) -> MCPServer:
    server: MCPServer = MCPServer(name="sculpt-workspace", title="Sculpt Workspace", instructions=INSTRUCTIONS)

    def absolute(payload: Any) -> Any:
        """Add absolute URLs next to relative artifact URLs (supplemental to image content)."""
        if isinstance(payload, dict):
            out = {k: absolute(v) for k, v in payload.items()}
            if isinstance(out.get("url"), str) and out["url"].startswith("/"):
                out["absolute_url"] = base_url + out["url"]
            return out
        if isinstance(payload, list):
            return [absolute(v) for v in payload]
        return payload

    @server.tool(
        description="Start here. With project_id: the current revision id, revision history (summaries, "
        "edit_job_id, objects added/modified/removed), the latest render job id and its views, the user's "
        "brief and reference list, and active jobs. Without project_id: the list of projects."
    )
    async def get_project(project_id: str | None = None) -> CallToolResult:
        async def run() -> CallToolResult:
            return _content(await get_ws().overview(project_id))

        return await _guard(run)

    @server.tool(
        description="The full Sculpt Workspace playbook: how to read the brief, target surfaces precisely, edit in "
        "small reviewed steps and fix rejected edits, with a worked example. Read it once before the first edit."
    )
    async def get_playbook() -> CallToolResult:
        async def run() -> CallToolResult:
            guide = resources.files("sculpt_workspace").joinpath("agent_guide.md").read_text(encoding="utf-8")
            return CallToolResult(content=[TextContent(type="text", text=guide)])

        return await _guard(run)

    @server.tool(
        description="Inspect a revision (default: current). Without `query`, returns the user's brief and reference "
        "list plus cached scene data immediately (object ids, names, types, mesh counts, world transforms, "
        "bounds, dimensions, units, visibility, protection); include_references=true attaches the reference "
        "images. With a bounded spatial `query`, returns an inspection job_id for get_result. Query "
        "{type: 'measure', object_id, target_ids?} reports the object's size (also in mm), its closest "
        "distance to the targets (default: the original sculpture) and intersecting face pairs."
    )
    async def inspect_project(
        project_id: str,
        revision_id: str | None = None,
        query: Annotated[SpatialQuery | None, Field(description="Optional bounded spatial query")] = None,
        include_references: Annotated[
            bool, Field(description="Attach the user's reference images (cached inspection only)")
        ] = False,
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            req = InspectRequest(
                project_id=project_id, revision_id=revision_id, query=query, include_references=include_references
            )
            ws = get_ws()
            info = await ws.inspect(req)
            images = await ws.reference_images(project_id) if include_references and query is None else None
            return _content(absolute(info), images)

        return await _guard(run)

    @server.tool(description=EDIT_DESCRIPTION)
    async def apply_edit(
        project_id: str,
        base_revision_id: str,
        source: str | None = None,
        summary: str | None = None,
        dry_run: bool = False,
        strict_checks: bool = False,
        from_dry_run: str | None = None,
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            req = EditRequest(
                project_id=project_id,
                base_revision_id=base_revision_id,
                source=source,
                summary=summary,
                dry_run=dry_run,
                strict_checks=strict_checks,
                from_dry_run=from_dry_run,
            )
            return _content(await get_ws().submit_edit(req))

        return await _guard(run)

    @server.tool(
        description="Render a revision (default: current) using named views (front, right, back, left, top, "
        "perspective) or explicit cameras. Default is the four standard views in Workbench at 1024 px; "
        "engine='eevee' is available. Pass `frame` {front, up} to change the viewing frame without rotating "
        "the sculpture, and persist_frame=true to keep it for later renders. `focus_object_ids` renders the "
        "views as close-ups framed on those objects (named closeup_<view>). `passes` adds images per view: "
        "'highlight' (orange = changed in this revision, blue = earlier agent objects, grey = original), "
        "'normal' (world normals as RGB, magenta = inverted), 'depth' (near = bright); they are named "
        "<view>--<pass>. Returns a job_id."
    )
    async def render_views(
        project_id: str,
        revision_id: str | None = None,
        views: list[NamedView] | None = None,
        cameras: list[CameraSpec] | None = None,
        engine: Literal["workbench", "eevee"] = "workbench",
        resolution: int = 1024,
        frame: ViewFrame | None = None,
        persist_frame: bool = False,
        passes: list[ExtraPass] | None = None,
        focus_object_ids: list[str] | None = None,
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            req = RenderRequest(
                project_id=project_id,
                revision_id=revision_id,
                views=views,
                cameras=cameras,
                engine=engine,
                resolution=resolution,
                frame=frame,
                persist_frame=persist_frame,
                passes=passes,
                focus_object_ids=focus_object_ids,
            )
            return _content(await get_ws().render(req))

        return await _guard(run)

    @server.tool(
        description="Return a job's state, reports, revision and artifact ids. For render jobs (and edits/"
        "imports whose render finished, and dry-run edits) returns bounded PNG previews as image content "
        "(max_image_size px, at most 8 images): by default the colour image of every rendered view. Request "
        "specific `views` and `passes` (color, highlight, normal, depth), or a `crop` {view, x, y, width, "
        "height} in normalised top-left coordinates for a higher-resolution detail (view may name a pass, "
        "e.g. front--highlight). overlays=true draws a world grid with axis values and a scale bar "
        "(orthographic views), an axis gizmo and the view name. compare=true pairs each standard view with "
        "the previous revision's render, before | after. wait_seconds (max 50) waits until the job, and "
        "the render an edit queues, has finished; check still_running. include_source=true adds an edit's "
        "script. Results are compact by default; detail='full' adds artifact metadata (camera matrices, bounds)."
    )
    async def get_result(
        job_id: str,
        include_images: bool = True,
        views: list[str] | None = None,
        max_image_size: int = 512,
        crop: Crop | None = None,
        passes: list[PassName] | None = None,
        overlays: bool = False,
        compare: bool = False,
        wait_seconds: float = 0.0,
        detail: Literal["compact", "full"] = "compact",
        include_source: bool = False,
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            req = ResultRequest(
                job_id=job_id,
                include_images=include_images,
                views=views,
                max_image_size=max_image_size,
                crop=crop,
                passes=passes or ["color"],
                overlays=overlays,
                compare=compare,
                wait_seconds=wait_seconds,
                detail=detail,
                include_source=include_source,
            )
            info, images = await get_ws().result(req)
            return _content(absolute(info), images)

        return await _guard(run)

    @server.tool(
        description="Make an existing revision current. Requires expected_current_revision_id to match the "
        "project's current revision. No revision is ever deleted; the next edit becomes a child of it."
    )
    async def checkout_revision(project_id: str, revision_id: str, expected_current_revision_id: str) -> CallToolResult:
        async def run() -> CallToolResult:
            req = CheckoutRequest(
                project_id=project_id,
                revision_id=revision_id,
                expected_current_revision_id=expected_current_revision_id,
            )
            return _content(await get_ws().checkout(req))

        return await _guard(run)

    @server.tool(
        description="Export an explicit revision. format='blend' returns the self-contained file immediately; "
        "format='stl' starts a job (visible meshes, evaluated modifiers and world transforms applied; "
        "stl_units 'mm' (default), 'm', or 'scene'). Exports never modify revisions."
    )
    async def export_model(
        project_id: str,
        revision_id: str,
        format: Literal["blend", "stl"],
        stl_units: Literal["mm", "m", "scene"] = "mm",
    ) -> CallToolResult:
        async def run() -> CallToolResult:
            req = ExportRequest(project_id=project_id, revision_id=revision_id, format=format, stl_units=stl_units)
            return _content(absolute(await get_ws().export(req)))

        return await _guard(run)

    @server.prompt(
        title="Sculpt task",
        description="Playbook for adding to a sculpture: understand the brief, target precisely, edit in small "
        "reviewed steps, and fix rejections. Includes a worked example.",
    )
    def sculpt_task(project_id: str, goal: str = "") -> str:
        guide = resources.files("sculpt_workspace").joinpath("agent_guide.md").read_text(encoding="utf-8")
        task = f"Project: {project_id}\nGoal: {goal or 'see the project brief (get_project)'}\n\n"
        return task + guide

    return server
