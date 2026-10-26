"""Bounded spatial queries against evaluated, world-space geometry.

See ``world_mesh`` for how geometry is evaluated. Face references are the
polygon index of the evaluated mesh, scoped to the revision that was queried.
"""

import bisect
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import ScriptError, depsgraph, open_blend, run
from mathutils import Vector  # type: ignore[import-not-found]
from world_mesh import SurfaceSet, face_ref, load_meshes
from world_mesh import vec as _vec

MEASURE_SAMPLES = 4000


def ray_query(meshes, rev, origin, direction, max_distance):
    o, d = Vector(origin), Vector(direction).normalized()
    dist = max_distance if max_distance else 1.0e12
    best = None
    for m in meshes:
        loc, normal, index, distance = m.bvh.ray_cast(o, d, dist)
        if loc is not None and (best is None or distance < best[3]):
            best = (m, loc, normal, distance, index)
    base = {"origin": _vec(origin), "direction": _vec(d), "max_distance": max_distance}
    if best is None:
        return {**base, "hit": False, "reason": "miss: the ray does not intersect any queried surface"}
    m, loc, normal, distance, index = best
    if normal.dot(d) > 0:
        facing = "back"
    else:
        facing = "front"
    return {
        **base,
        "hit": True,
        "position": _vec(loc),
        "normal": _vec(normal.normalized()),
        "distance": round(float(distance), 7),
        "facing": facing,
        "object_name": m.name,
        "face": face_ref(rev, m.id, index),
    }


def nearest_query(meshes, rev, point, max_distance):
    p = Vector(point)
    best = None
    for m in meshes:
        hit = m.bvh.find_nearest(p, max_distance) if max_distance else m.bvh.find_nearest(p)
        loc, normal, index, distance = hit
        if loc is not None and (best is None or distance < best[3]):
            best = (m, loc, normal, distance, index)
    if best is None:
        return {"point": _vec(point), "hit": False, "reason": "miss: no surface within max_distance"}
    m, loc, normal, distance, index = best
    return {
        "point": _vec(point),
        "hit": True,
        "position": _vec(loc),
        "normal": _vec(normal.normalized()),
        "distance": round(float(distance), 7),
        "object_name": m.name,
        "face": face_ref(rev, m.id, index),
    }


def region_query(meshes, rev, lo, hi, limit):
    lo_a, hi_a = np.array(lo), np.array(hi)
    center = (lo_a + hi_a) / 2.0
    objects = []
    faces = []
    total = 0
    for m in meshes:
        if np.any(m.bounds_max < lo_a) or np.any(m.bounds_min > hi_a):
            continue
        inside = np.all((m.centers >= lo_a) & (m.centers <= hi_a), axis=1)
        idx = np.nonzero(inside)[0]
        total += len(idx)
        objects.append(
            {
                "object_id": m.id,
                "name": m.name,
                "faces_in_region": len(idx),
                "bounds": {"min": _vec(m.bounds_min), "max": _vec(m.bounds_max)},
            }
        )
        for i in idx:
            faces.append((float(np.linalg.norm(m.centers[i] - center)), m, int(i)))
    faces.sort(key=lambda f: (f[0], f[1].id, f[2]))
    if len(faces) > limit:
        # Evenly stride through the distance-ordered list so the sample spans the region.
        step = len(faces) / limit
        faces = [faces[int(k * step)] for k in range(limit)]
    return {
        "region": {"min": _vec(lo), "max": _vec(hi)},
        "objects": objects,
        "faces": [
            {
                "position": _vec(m.centers[i]),
                "normal": _vec(m.normals[i]),
                "distance": round(d, 7),
                "object_name": m.name,
                "face": face_ref(rev, m.id, i),
            }
            for d, m, i in faces
        ],
        "total_matches": int(total),
        "returned": len(faces),
        "truncated": total > len(faces),
        "limit": limit,
        "note": "Faces are selected by polygon centre; distance is from the region centre.",
    }


def samples_query(meshes, rev, count, seed):
    rng = random.Random(seed)  # noqa: S311 - deterministic sampling, not security
    tris = []
    for m in meshes:
        if not len(m.tris):
            continue
        a, b, c = (m.verts[m.tris[:, k]] for k in range(3))
        cr = np.cross(b - a, c - a)
        areas = 0.5 * np.linalg.norm(cr, axis=1)
        tris.append((m, a, b, c, cr, areas))
    if not tris:
        raise ScriptError("no_surface", "No surface to sample.")
    cum = []
    acc = 0.0
    for t in tris:
        acc += float(t[5].sum())
        cum.append(acc)
    total_area = acc
    samples = []
    for _ in range(count):
        r = rng.random() * total_area
        k = min(bisect.bisect_right(cum, r), len(tris) - 1)
        m, a, b, c, cr, areas = tris[k]
        local_cum = np.cumsum(areas)
        target = rng.random() * float(local_cum[-1])
        t = int(min(np.searchsorted(local_cum, target, side="right"), len(areas) - 1))
        r1, r2 = rng.random(), rng.random()
        s = math.sqrt(r1)
        pos = (1 - s) * a[t] + s * (1 - r2) * b[t] + s * r2 * c[t]
        n = cr[t]
        nl = np.linalg.norm(n)
        samples.append(
            {
                "position": _vec(pos),
                "normal": _vec(n / nl if nl > 0 else n),
                "object_name": m.name,
                "face": face_ref(rev, m.id, int(m.tri_poly[t])),
            }
        )
    return {
        "samples": samples,
        "seed": seed,
        "returned": len(samples),
        "total_area": round(total_area, 7),
        "note": "Area-weighted, deterministic for a given seed and revision.",
    }


def _mm(units: float, scale_length: float) -> float:
    return round(float(units) * scale_length * 1000.0, 4)


def measure_query(dg, rev, q, protected_ids):
    """Size of one object and its closest approach to, and overlap with, the targets."""
    (obj,) = load_meshes(dg, [q["object_id"]])
    target_ids = q.get("target_ids") or [i for i in protected_ids if i != q["object_id"]]
    if not target_ids:
        raise ScriptError("no_targets", "There is nothing to measure against.")
    # Hidden protected objects are not targets by default; explicit targets may be hidden.
    targets = (
        load_meshes(dg, target_ids) if q.get("target_ids") else [m for m in load_meshes(dg) if m.id in set(target_ids)]
    )
    targets = [m for m in targets if m.id != obj.id]
    if not targets:
        raise ScriptError("no_targets", "None of the targets has visible surface geometry.")
    us = bpy.context.scene.unit_settings
    scale_length = float(us.scale_length) or 1.0
    size = obj.bounds_max - obj.bounds_min
    surface = SurfaceSet(targets)
    step = max(1, len(obj.verts) // MEASURE_SAMPLES)
    best = None
    for p in obj.verts[::step]:
        hit = surface.nearest(p.tolist())
        if hit is not None and (best is None or hit["distance"] < best[0]):
            best = (hit["distance"], p, hit)
    overlaps = obj.bvh.overlap(surface.bvh) if surface.bvh is not None else []
    result = {
        "object": {
            "object_id": obj.id,
            "name": obj.name,
            "bounds": {"min": _vec(obj.bounds_min), "max": _vec(obj.bounds_max)},
            "size": _vec(size),
            "size_mm": [_mm(c, scale_length) for c in size],
            "diagonal": round(float(np.linalg.norm(size)), 7),
        },
        "targets": [{"object_id": m.id, "name": m.name} for m in targets],
        "scale_length": scale_length,
        "intersecting_face_pairs": len(overlaps),
        "touching": bool(overlaps) or (best is not None and best[0] <= 1e-6),
        "note": "Distances are Blender units (mm = units * scale_length * 1000). The closest distance is "
        "sampled from the object's vertices, so it is an upper bound for very coarse meshes.",
    }
    if best is not None:
        distance, point, hit = best
        result["closest"] = {
            "distance": round(distance, 7),
            "distance_mm": _mm(distance, scale_length),
            "object_point": _vec(point),
            "target_point": _vec(hit["position"]),
            "target_normal": _vec(hit["normal"]),
            "target_object_id": hit["object_id"],
            "face": face_ref(rev, hit["object_id"], hit["face_index"]),
        }
    return result


def main(request: dict) -> dict:
    open_blend(request["model"])
    dg = depsgraph()
    q = request["query"]
    rev = request["revision_id"]
    if q["type"] == "measure":
        return {"query_type": "measure", "result": measure_query(dg, rev, q, request.get("protected_ids") or [])}
    meshes = load_meshes(dg, q.get("object_ids"))
    if not meshes:
        raise ScriptError("no_surface", "No visible surface geometry to query.")
    kind = q["type"]
    if kind in ("ray", "image_ray"):
        result = ray_query(meshes, rev, q["origin"], q["direction"], q.get("max_distance"))
    elif kind == "nearest":
        result = nearest_query(meshes, rev, q["point"], q.get("max_distance"))
    elif kind == "region":
        result = region_query(meshes, rev, q["min"], q["max"], int(q["limit"]))
    elif kind == "surface_samples":
        result = samples_query(meshes, rev, int(q["count"]), int(q["seed"]))
    else:
        raise ScriptError("invalid_query", f"Unknown query type {kind!r}")
    return {"query_type": kind, "result": result}


run(main)
