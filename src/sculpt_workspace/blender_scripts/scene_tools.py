"""Fingerprinting, self-containment checks, and scene inspection inside Blender.

Fingerprints are per-object dictionaries of SHA-256 digests, one per protected
component, so the server can report exactly which aspect of a protected
object changed.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import object_id

GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "FONT", "META"}
TRANSFORM_ONLY_TYPES = {"EMPTY", "CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER", "ARMATURE", "LATTICE"}
EVAL_DECIMALS = 5

# Attribute data types -> (foreach field, components, numpy dtype)
ATTRIBUTE_LAYOUT: dict[str, tuple[str, int, Any]] = {
    "FLOAT": ("value", 1, np.float32),
    "INT": ("value", 1, np.int32),
    "INT8": ("value", 1, np.int8),
    "BOOLEAN": ("value", 1, np.bool_),
    "FLOAT2": ("vector", 2, np.float32),
    "INT32_2D": ("value", 2, np.int32),
    "INT16_2D": ("value", 2, np.int16),
    "FLOAT_VECTOR": ("vector", 3, np.float32),
    "FLOAT_COLOR": ("color", 4, np.float32),
    "BYTE_COLOR": ("color", 4, np.float32),
    "QUATERNION": ("value", 4, np.float32),
    "FLOAT4X4": ("value", 16, np.float32),
}
# Editor state stored as attributes; changing selection must not count as an edit.
UI_STATE_PREFIXES = (".select", ".hide", ".uv_select", ".active")
SKIP_RNA = {
    "rna_type",
    "show_expanded",
    "is_active",
    "persistent_uid",
    "is_override_data_editable",
    "show_in_editmode",
    "show_on_cage",
    "is_pin_to_last",
    "use_pin_to_last",
}


class Unreliable(Exception):
    """A component that cannot be fingerprinted deterministically."""


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, np.ndarray):
            h.update(str(p.dtype).encode())
            h.update(str(p.shape).encode())
            h.update(np.ascontiguousarray(p).tobytes())
        else:
            h.update(repr(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _get(collection: Any, field: str, count: int, width: int, dtype: Any) -> np.ndarray:
    arr = np.empty(count * width, dtype=dtype)
    if count:
        collection.foreach_get(field, arr)
    return arr


def _topology(me: Any) -> str:
    nv, ne, nl, npoly = len(me.vertices), len(me.edges), len(me.loops), len(me.polygons)
    return _digest(
        (nv, ne, nl, npoly),
        _get(me.edges, "vertices", ne, 2, np.int32),
        _get(me.loops, "vertex_index", nl, 1, np.int32),
        _get(me.polygons, "loop_start", npoly, 1, np.int32),
        _get(me.polygons, "loop_total", npoly, 1, np.int32),
    )


def _coordinates(me: Any) -> str:
    return _digest(_get(me.vertices, "co", len(me.vertices), 3, np.float32))


def _attributes(me: Any) -> str:
    parts: list[Any] = []
    for attr in sorted(me.attributes, key=lambda a: a.name):
        if attr.name.startswith(UI_STATE_PREFIXES):
            continue
        layout = ATTRIBUTE_LAYOUT.get(attr.data_type)
        if layout is None:
            if attr.data_type == "STRING":
                parts.append((attr.name, attr.domain, attr.data_type, tuple(d.value for d in attr.data)))
                continue
            raise Unreliable(f"attribute {attr.name!r} has unsupported type {attr.data_type}")
        field, width, dtype = layout
        parts.append((attr.name, attr.domain, attr.data_type))
        parts.append(_get(attr.data, field, len(attr.data), width, dtype))
    return _digest(*parts)


def _shape_keys(me: Any) -> str:
    key = me.shape_keys
    if key is None:
        return _digest("none")
    parts: list[Any] = [key.use_relative, key.reference_key.name if key.reference_key else None]
    for kb in key.key_blocks:
        parts.append(
            (
                kb.name,
                round(kb.value, 7),
                kb.mute,
                kb.relative_key.name if kb.relative_key else None,
                kb.slider_min,
                kb.slider_max,
                kb.vertex_group,
                kb.interpolation,
            )
        )
        parts.append(_get(kb.data, "co", len(kb.data), 3, np.float32))
    return _digest(*parts)


def _rna_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 7)
    if isinstance(value, bpy.types.ID):
        oid = object_id(value) if isinstance(value, bpy.types.Object) else None
        return ("ID", type(value).__name__, oid or value.name)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(str(v) for v in value))  # enum flags: order-independent
    try:
        return tuple(_rna_value(v) for v in value)
    except TypeError:
        return repr(value)


def rna_dump(struct: Any) -> tuple[Any, ...]:
    out: list[Any] = []
    for prop in struct.bl_rna.properties:
        ident = prop.identifier
        if ident in SKIP_RNA or prop.type == "COLLECTION":
            continue
        try:
            value = getattr(struct, ident)
        except AttributeError:
            continue
        if prop.type == "POINTER" and value is not None and not isinstance(value, bpy.types.ID):
            continue
        out.append((ident, _rna_value(value)))
    return tuple(out)


def _vertex_groups(obj: Any) -> str:
    """Group names plus per-vertex weights (deform weights are mesh attributes)."""
    names = tuple((vg.name, vg.lock_weight) for vg in obj.vertex_groups)
    if not names:
        return _digest(names)
    weights = [tuple((g.group, round(g.weight, 6)) for g in v.groups) for v in obj.data.vertices]
    return _digest(names, weights)


def _modifiers(obj: Any) -> str:
    parts: list[Any] = []
    for mod in obj.modifiers:
        parts.append((mod.name, mod.type, rna_dump(mod)))
        id_props = {k: _rna_value(mod[k]) for k in mod.keys()} if hasattr(mod, "keys") else {}
        parts.append(tuple(sorted(id_props.items(), key=lambda kv: kv[0])))
        if mod.type == "NODES" and mod.node_group is not None:
            parts.append(("node_group", mod.node_group.name))
    return _digest(*parts)


def _transform(obj: Any, obj_eval: Any) -> str:
    mw = [round(v, 6) for row in obj_eval.matrix_world for v in row]
    parent = obj.parent
    return _digest(
        mw,
        [round(v, 7) for v in obj.location],
        obj.rotation_mode,
        [round(v, 7) for v in obj.rotation_euler],
        [round(v, 7) for v in obj.rotation_quaternion],
        [round(v, 7) for v in obj.scale],
        [round(v, 7) for v in obj.delta_location],
        [round(v, 7) for v in obj.delta_rotation_euler],
        [round(v, 7) for v in obj.delta_scale],
        [round(v, 7) for row in obj.matrix_parent_inverse for v in row],
        (object_id(parent) if parent else None, obj.parent_type, obj.parent_bone),
        tuple((c.type, c.name, rna_dump(c)) for c in obj.constraints),
    )


def _evaluated(obj_eval: Any) -> str:
    try:
        me = obj_eval.to_mesh()
    except RuntimeError as exc:
        raise Unreliable(f"evaluated geometry unavailable: {exc}") from exc
    if me is None:
        return _digest("empty")
    try:
        co = _get(me.vertices, "co", len(me.vertices), 3, np.float32)
        co = np.round(co.astype(np.float64), EVAL_DECIMALS)
        co[co == 0.0] = 0.0  # normalise negative zero
        return _digest(_topology(me), co)
    finally:
        obj_eval.to_mesh_clear()


def fingerprint_object(obj: Any, dg: Any) -> dict[str, Any]:
    obj_eval = obj.evaluated_get(dg)
    fp: dict[str, Any] = {"name": obj.name, "type": obj.type, "components": {}, "unreliable": []}
    comps = fp["components"]

    def capture(name: str, fn: Any) -> None:
        try:
            comps[name] = fn()
        except Unreliable as exc:
            fp["unreliable"].append({"component": name, "reason": str(exc)})

    capture("transform", lambda: _transform(obj, obj_eval))
    capture("modifiers", lambda: _modifiers(obj))
    if obj.type == "MESH":
        me = obj.data
        comps["data_name"] = _digest(me.name, me.users > 1)
        capture("topology", lambda: _topology(me))
        capture("coordinates", lambda: _coordinates(me))
        capture("attributes", lambda: _attributes(me))
        capture("shape_keys", lambda: _shape_keys(me))
        capture("vertex_groups", lambda: _vertex_groups(obj))
        capture("evaluated_geometry", lambda: _evaluated(obj_eval))
    elif obj.type in GEOMETRY_TYPES:
        capture("data", lambda: _digest(rna_dump(obj.data)) if obj.data is not None else _digest(None))
        capture("evaluated_geometry", lambda: _evaluated(obj_eval))
    elif obj.type in TRANSFORM_ONLY_TYPES:
        capture("data", lambda: _digest(rna_dump(obj.data)) if obj.data is not None else _digest(None))
    else:
        fp["unreliable"].append({"component": "geometry", "reason": f"object type {obj.type} cannot be fingerprinted"})
    return fp


def fingerprint_all(dg: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for obj in bpy.data.objects:
        oid = object_id(obj)
        if oid is not None:
            out[oid] = fingerprint_object(obj, dg)
    return out


# -- self-containment ------------------------------------------------------


def self_contained_issues() -> list[dict[str, str]]:
    """List every dependency that lives outside the .blend file."""
    issues: list[dict[str, str]] = []

    def add(code: str, kind: str, name: str, message: str) -> None:
        issues.append({"code": code, "datablock": kind, "name": name, "message": message})

    for lib in bpy.data.libraries:
        add("linked_library", "library", lib.name, f"Linked library {lib.filepath!r} must be made local.")
    for img in bpy.data.images:
        if img.source in {"FILE", "SEQUENCE", "MOVIE", "TILED"} and img.packed_file is None and not img.packed_files:
            add("missing_asset", "image", img.name, f"Image {img.filepath!r} is not packed into the file.")
        if img.library is not None:
            add("linked_library", "image", img.name, "Image is linked from another file.")
    for font in bpy.data.fonts:
        if font.filepath != "<builtin>" and font.packed_file is None:
            add("missing_asset", "font", font.name, f"Font {font.filepath!r} is not packed into the file.")
    for sound in bpy.data.sounds:
        if sound.packed_file is None:
            add("missing_asset", "sound", sound.name, f"Sound {sound.filepath!r} is not packed into the file.")
    for vol in getattr(bpy.data, "volumes", []):
        if vol.packed_file is None and vol.filepath:
            add("unsupported_dependency", "volume", vol.name, "OpenVDB volumes reference external files.")
    for cache in bpy.data.cache_files:
        add("unsupported_dependency", "cache_file", cache.name, f"Cache file {cache.filepath!r} is external.")
    for clip in bpy.data.movieclips:
        add("unsupported_dependency", "movie_clip", clip.name, "Movie clips reference external files.")
    for obj in bpy.data.objects:
        if obj.library is not None or obj.override_library is not None:
            add("linked_library", "object", obj.name, "Object is linked or overridden from another file.")
        for mod in obj.modifiers:
            if mod.type in {"MESH_SEQUENCE_CACHE"}:
                add(
                    "unsupported_dependency",
                    "modifier",
                    f"{obj.name}/{mod.name}",
                    "Mesh cache modifiers read external files.",
                )
            if mod.type == "MESH_CACHE":
                add(
                    "unsupported_dependency",
                    "modifier",
                    f"{obj.name}/{mod.name}",
                    "Mesh cache modifiers read external files.",
                )
            if mod.type == "FLUID" or (mod.type == "OCEAN" and getattr(mod, "is_cached", False)):
                add(
                    "unsupported_dependency",
                    "modifier",
                    f"{obj.name}/{mod.name}",
                    "Baked simulation caches live on disk.",
                )
        for psys in getattr(obj, "particle_systems", []):
            cache = psys.point_cache
            if cache.use_external or cache.use_disk_cache:
                add(
                    "unsupported_dependency",
                    "point_cache",
                    f"{obj.name}/{psys.name}",
                    "Particle cache is stored on disk.",
                )
    return issues


# -- inspection -------------------------------------------------------------


def _world_bounds(obj_eval: Any) -> tuple[list[float], list[float]] | None:
    mw = np.array(obj_eval.matrix_world, dtype=np.float64)
    if obj_eval.type == "MESH" or obj_eval.type in GEOMETRY_TYPES:
        try:
            me = obj_eval.to_mesh()
        except RuntimeError:
            me = None
        if me is not None:
            try:
                n = len(me.vertices)
                if n:
                    co = _get(me.vertices, "co", n, 3, np.float32).reshape(n, 3).astype(np.float64)
                    world = co @ mw[:3, :3].T + mw[:3, 3]
                    return world.min(axis=0).tolist(), world.max(axis=0).tolist()
            finally:
                obj_eval.to_mesh_clear()
    corners = np.array([list(c) for c in obj_eval.bound_box], dtype=np.float64)
    if not len(corners):
        return None
    world = corners @ mw[:3, :3].T + mw[:3, 3]
    return world.min(axis=0).tolist(), world.max(axis=0).tolist()


def mesh_counts(obj: Any, obj_eval: Any) -> dict[str, int] | None:
    if obj.type != "MESH":
        return None
    me = obj.data
    counts = {"vertices": len(me.vertices), "edges": len(me.edges), "faces": len(me.polygons)}
    try:
        ev = obj_eval.to_mesh()
        if ev is not None:
            ev.calc_loop_triangles()
            counts.update(
                {
                    "evaluated_vertices": len(ev.vertices),
                    "evaluated_faces": len(ev.polygons),
                    "evaluated_triangles": len(ev.loop_triangles),
                }
            )
    finally:
        obj_eval.to_mesh_clear()
    return counts


def inspect_scene(dg: Any) -> dict[str, Any]:
    scene = bpy.context.scene
    us = scene.unit_settings
    objects: list[dict[str, Any]] = []
    scene_min = [math.inf] * 3
    scene_max = [-math.inf] * 3
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        oid = object_id(obj)
        obj_eval = obj.evaluated_get(dg)
        bounds = _world_bounds(obj_eval)
        visible = bool(obj.visible_get())
        entry: dict[str, Any] = {
            "id": oid,
            "name": obj.name,
            "type": obj.type,
            "data_name": obj.data.name if obj.data is not None else None,
            "parent_id": object_id(obj.parent) if obj.parent else None,
            "visible": visible,
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "matrix_world": [[round(v, 7) for v in row] for row in obj_eval.matrix_world],
            "location": [round(v, 7) for v in obj.location],
            "rotation_mode": obj.rotation_mode,
            "rotation_euler": [round(v, 7) for v in obj.rotation_euler],
            "scale": [round(v, 7) for v in obj.scale],
            "dimensions": [round(v, 7) for v in obj.dimensions],
            "bounds": {"min": bounds[0], "max": bounds[1]} if bounds else None,
            "mesh": mesh_counts(obj, obj_eval),
            "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
            "materials": [s.material.name if s.material else None for s in obj.material_slots],
        }
        objects.append(entry)
        if bounds and visible and obj.type in GEOMETRY_TYPES:
            for i in range(3):
                scene_min[i] = min(scene_min[i], bounds[0][i])
                scene_max[i] = max(scene_max[i], bounds[1][i])
    has_bounds = all(math.isfinite(v) for v in scene_min + scene_max)
    return {
        "scene": {
            "name": scene.name,
            "frame_current": scene.frame_current,
            "units": {
                "system": us.system,
                "scale_length": us.scale_length,
                "length_unit": us.length_unit,
                "note": "World coordinates are Blender units; metres = units * scale_length.",
            },
            "up_axis": "Z",
            "bounds": {"min": scene_min, "max": scene_max} if has_bounds else None,
        },
        "objects": objects,
    }
