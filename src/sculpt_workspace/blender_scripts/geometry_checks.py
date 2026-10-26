"""Sanity checks on agent-created geometry.

Each agent (unprotected) object gets a list of issues with a severity:
``error`` (always rejects the edit), ``warning`` (rejects only when the agent
asks for strict checks) or ``info``. Checks never look at protected objects
except as the reference surface.
"""

from __future__ import annotations

from typing import Any

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import object_id
from scene_tools import GEOMETRY_TYPES
from world_mesh import SurfaceSet, WorldMesh, load_meshes

MAX_CHECKED_OBJECTS = 100
MAX_DISTANCE_SAMPLES = 2000
ATTACH_TOLERANCE = 0.01  # fraction of the sculpture's diagonal
TOO_LARGE_RATIO = 3.0
TOO_SMALL_RATIO = 0.002


def _issue(code: str, severity: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **extra}


def _edge_counts(polys: list[list[int]]) -> tuple[int, int]:
    """(open boundary edges, non-manifold edges shared by 3+ faces)."""
    pairs = [(p[i], p[(i + 1) % len(p)]) for p in polys for i in range(len(p))]
    if not pairs:
        return 0, 0
    edges = np.sort(np.array(pairs, dtype=np.int64), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return int(np.count_nonzero(counts == 1)), int(np.count_nonzero(counts > 2))


def _signed_volume(wm: WorldMesh) -> float:
    if not len(wm.tris):
        return 0.0
    a, b, c = (wm.verts[wm.tris[:, k]] for k in range(3))
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def _gap(a_min: np.ndarray, a_max: np.ndarray, b_min: np.ndarray, b_max: np.ndarray) -> float:
    d = np.maximum(0.0, np.maximum(b_min - a_max, a_min - b_max))
    return float(np.linalg.norm(d))


def _min_distance(wm: WorldMesh, surface: SurfaceSet) -> float:
    step = max(1, len(wm.verts) // MAX_DISTANCE_SAMPLES)
    best = float("inf")
    for p in wm.verts[::step]:
        hit = surface.nearest(p.tolist())
        if hit is not None:
            best = min(best, hit["distance"])
    return best


def check_objects(dg: Any, protected_ids: set[str]) -> list[dict[str, Any]]:
    sculpture = SurfaceSet([m for m in load_meshes(dg) if m.id in protected_ids])
    diag = sculpture.diagonal
    tol = ATTACH_TOLERANCE * diag
    agent: list[tuple[Any, WorldMesh | None, list[dict[str, Any]]]] = []
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        oid = object_id(obj)
        if oid is None or oid in protected_ids or obj.type not in GEOMETRY_TYPES:
            continue
        if len(agent) >= MAX_CHECKED_OBJECTS:
            break
        issues: list[dict[str, Any]] = []
        wm: WorldMesh | None = None
        try:
            wm = WorldMesh(obj, dg)
        except RuntimeError as exc:
            issues.append(_issue("not_evaluable", "error", f"Geometry could not be evaluated: {exc}"))
        if wm is not None and (not len(wm.verts) or not wm.polys):
            issues.append(_issue("empty", "warning", "The object has no faces, so it renders and exports as nothing."))
            wm = None
        if wm is not None and not np.all(np.isfinite(wm.verts)):
            issues.append(_issue("non_finite", "error", "Some vertex coordinates are NaN or infinite."))
            wm = None
        if not obj.visible_get() or obj.hide_render:
            issues.append(_issue("hidden", "info", "The object is hidden, so renders and STL exports skip it."))
        agent.append((obj, wm, issues))

    for obj, wm, issues in agent:
        if wm is None:
            continue
        size = float(np.linalg.norm(wm.bounds_max - wm.bounds_min))
        if diag > 0:
            ratio = size / diag
            if ratio > TOO_LARGE_RATIO:
                issues.append(
                    _issue(
                        "too_large",
                        "warning",
                        f"The object is {ratio:.1f}x the size of the sculpture. Check units: coordinates are "
                        "Blender units, see inspect_project scene.units.",
                        size=round(size, 6),
                        sculpture_size=round(diag, 6),
                    )
                )
            elif ratio < TOO_SMALL_RATIO:
                issues.append(
                    _issue(
                        "too_small",
                        "warning",
                        f"The object is only {ratio:.2%} of the sculpture's size and may be invisible in renders.",
                        size=round(size, 6),
                        sculpture_size=round(diag, 6),
                    )
                )
        open_edges, nonmanifold = _edge_counts(wm.polys)
        if open_edges or nonmanifold:
            issues.append(
                _issue(
                    "non_manifold",
                    "warning",
                    f"{open_edges} open boundary edge(s) and {nonmanifold} edge(s) shared by 3+ faces. "
                    "The mesh is not watertight, which breaks 3D printing from STL.",
                    open_edges=open_edges,
                    non_manifold_edges=nonmanifold,
                )
            )
        elif _signed_volume(wm) < 0:
            issues.append(
                _issue(
                    "inverted_normals",
                    "warning",
                    "The closed mesh has a negative volume: its normals point inward. Flip them with "
                    "bmesh.ops.reverse_faces.",
                )
            )
        if sculpture.empty:
            continue
        touching = _min_distance(wm, sculpture)
        if touching > tol:
            near_other = any(
                other is not obj
                and ow is not None
                and _gap(wm.bounds_min, wm.bounds_max, ow.bounds_min, ow.bounds_max) <= tol
                for other, ow, _ in agent
            )
            if not near_other:
                issues.append(
                    _issue(
                        "floating",
                        "warning",
                        f"The object is {touching:.4g} units from the sculpture and touches nothing else. Attach it "
                        "with sculpt.nearest() / sculpt.ray() and sculpt.place_on_surface().",
                        distance=round(touching, 6),
                        tolerance=round(tol, 6),
                    )
                )
        overlaps = wm.bvh.overlap(sculpture.bvh) if sculpture.bvh is not None else []
        if overlaps:
            issues.append(
                _issue(
                    "intersects_original",
                    "info",
                    f"{len(overlaps)} face pair(s) cut through the original sculpture. Fine for embedded "
                    "parts; otherwise lift the object with a positive offset.",
                    face_pairs=len(overlaps),
                )
            )
    return [{"object_id": object_id(obj), "name": obj.name, "issues": issues} for obj, _, issues in agent if issues]
