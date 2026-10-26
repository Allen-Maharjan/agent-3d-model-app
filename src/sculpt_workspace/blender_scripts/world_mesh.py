"""Evaluated, world-space meshes and BVH surface lookups inside Blender.

Shared by spatial queries, edit helpers, geometry checks and analysis render
passes. Geometry is evaluated (modifiers applied) and transformed to world
space before BVH construction, so hits, distances and face normals are
world-space values that stay correct under non-uniform scaling.
"""

from __future__ import annotations

from typing import Any

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import ScriptError, object_id
from mathutils import Vector  # type: ignore[import-not-found]
from mathutils.bvhtree import BVHTree  # type: ignore[import-not-found]
from scene_tools import GEOMETRY_TYPES


class WorldMesh:
    def __init__(self, obj: Any, dg: Any):
        self.id = object_id(obj)
        self.name = obj.name
        obj_eval = obj.evaluated_get(dg)
        me = obj_eval.to_mesh()
        try:
            me.calc_loop_triangles()
            nv, npoly, nt = len(me.vertices), len(me.polygons), len(me.loop_triangles)
            co = np.empty(nv * 3, np.float32)
            me.vertices.foreach_get("co", co)
            mw = np.array(obj_eval.matrix_world, dtype=np.float64)
            self.m3 = mw[:3, :3]
            self.t = mw[:3, 3]
            self.normal_matrix = np.linalg.inv(self.m3).T if abs(np.linalg.det(self.m3)) > 1e-18 else None
            self.verts = co.reshape(nv, 3).astype(np.float64) @ self.m3.T + self.t
            loop_v = np.empty(len(me.loops), np.int32)
            me.loops.foreach_get("vertex_index", loop_v)
            starts = np.empty(npoly, np.int32)
            totals = np.empty(npoly, np.int32)
            me.polygons.foreach_get("loop_start", starts)
            me.polygons.foreach_get("loop_total", totals)
            self.polys = [loop_v[s : s + n].tolist() for s, n in zip(starts, totals, strict=True)]
            centers = np.empty(npoly * 3, np.float32)
            normals = np.empty(npoly * 3, np.float32)
            me.polygons.foreach_get("center", centers)
            me.polygons.foreach_get("normal", normals)
            self.centers = centers.reshape(npoly, 3).astype(np.float64) @ self.m3.T + self.t
            self.normals = self._world_normals(normals.reshape(npoly, 3).astype(np.float64))
            tri_v = np.empty(nt * 3, np.int32)
            tri_p = np.empty(nt, np.int32)
            me.loop_triangles.foreach_get("vertices", tri_v)
            me.loop_triangles.foreach_get("polygon_index", tri_p)
            self.tris = tri_v.reshape(nt, 3)
            self.tri_poly = tri_p
        finally:
            obj_eval.to_mesh_clear()
        self.bounds_min = self.verts.min(axis=0) if len(self.verts) else None
        self.bounds_max = self.verts.max(axis=0) if len(self.verts) else None
        self._bvh = None

    def _world_normals(self, local: np.ndarray) -> np.ndarray:
        # Normals transform by the inverse transpose; this is what keeps them
        # perpendicular to surfaces under non-uniform scale.
        if self.normal_matrix is None or not len(local):
            return local
        n = local @ self.normal_matrix.T
        lens = np.linalg.norm(n, axis=1, keepdims=True)
        return np.divide(n, lens, out=np.zeros_like(n), where=lens > 0)

    @property
    def bvh(self) -> Any:
        if self._bvh is None:
            self._bvh = BVHTree.FromPolygons(self.verts.tolist(), self.polys, all_triangles=False)
        return self._bvh


def vec(v: Any) -> list:
    return [round(float(x), 7) for x in v]


def face_ref(revision_id: str, oid: str, index: int) -> dict:
    return {
        "revision_id": revision_id,
        "object_id": oid,
        "face_index": int(index),
        "ref": f"{revision_id}:{oid}:{int(index)}",
    }


def load_meshes(dg: Any, object_ids: Any = None, *, include_unidentified: bool = False) -> list[WorldMesh]:
    """World meshes for the given ids, or for every visible, rendered object.

    `include_unidentified` also loads visible objects that have no sculpt id
    yet (objects created by the running edit script).
    """
    wanted = set(object_ids) if object_ids else None
    meshes = []
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        oid = object_id(obj)
        if obj.type not in GEOMETRY_TYPES:
            continue
        if oid is None and not (include_unidentified and wanted is None):
            continue
        if wanted is not None and oid not in wanted:
            continue
        if wanted is None and (not obj.visible_get() or obj.hide_render):
            continue
        try:
            wm = WorldMesh(obj, dg)
        except RuntimeError:
            continue
        if len(wm.verts) and wm.polys:
            meshes.append(wm)
    if wanted is not None:
        missing = sorted(wanted - {m.id for m in meshes})
        if missing:
            raise ScriptError("unknown_objects", "Some object ids have no surface geometry.", {"object_ids": missing})
    return meshes


class SurfaceSet:
    """One BVH over several world meshes, for many ray casts against a scene."""

    def __init__(self, meshes: list[WorldMesh]):
        self.meshes = meshes
        verts: list[np.ndarray] = []
        polys: list[list[int]] = []
        self.poly_offsets: list[int] = []
        v_off = 0
        for m in meshes:
            self.poly_offsets.append(len(polys))
            verts.append(m.verts)
            polys.extend([[i + v_off for i in p] for p in m.polys])
            v_off += len(m.verts)
        self.empty = not polys
        all_verts = np.concatenate(verts) if verts else np.zeros((0, 3))
        self.bounds_min = all_verts.min(axis=0) if len(all_verts) else None
        self.bounds_max = all_verts.max(axis=0) if len(all_verts) else None
        self.bvh = None if self.empty else BVHTree.FromPolygons(all_verts.tolist(), polys, all_triangles=False)

    def _locate(self, index: int) -> tuple[WorldMesh, int]:
        k = int(np.searchsorted(self.poly_offsets, index, side="right")) - 1
        return self.meshes[k], index - self.poly_offsets[k]

    def _hit(self, loc: Any, normal: Any, index: Any, distance: Any) -> dict | None:
        if loc is None:
            return None
        mesh, face = self._locate(index)
        return {
            "position": Vector(loc),
            "normal": Vector(normal).normalized(),
            "distance": float(distance),
            "object_id": mesh.id,
            "object_name": mesh.name,
            "face_index": face,
        }

    def ray(self, origin: Any, direction: Any, max_distance: float | None = None) -> dict | None:
        if self.bvh is None:
            return None
        d = Vector(direction).normalized()
        return self._hit(*self.bvh.ray_cast(Vector(origin), d, max_distance or 1.0e12))

    def nearest(self, point: Any, max_distance: float | None = None) -> dict | None:
        if self.bvh is None:
            return None
        hit = (
            self.bvh.find_nearest(Vector(point), max_distance) if max_distance else self.bvh.find_nearest(Vector(point))
        )
        return self._hit(*hit)

    @property
    def diagonal(self) -> float:
        if self.bounds_min is None:
            return 0.0
        return float(np.linalg.norm(self.bounds_max - self.bounds_min))
