"""Export visible mesh objects of a revision as binary STL.

Evaluated modifiers and world transforms are applied to the export copy only;
the revision file is opened read-only in this process and never saved.
Units: STL has no unit field. Coordinates are written as Blender units
multiplied by scene.unit_settings.scale_length (giving metres), then by 1000
for "mm". "scene" writes raw Blender units.
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import ScriptError, depsgraph, object_id, open_blend, run


def main(request: dict) -> dict:
    open_blend(request["model"])
    dg = depsgraph()
    units = request.get("units", "mm")
    scale_length = bpy.context.scene.unit_settings.scale_length or 1.0
    factor = {"mm": scale_length * 1000.0, "m": scale_length, "scene": 1.0}[units]
    tris: list[np.ndarray] = []
    exported: list[str] = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or not obj.visible_get() or obj.hide_render:
            continue
        obj_eval = obj.evaluated_get(dg)
        me = obj_eval.to_mesh()
        try:
            me.calc_loop_triangles()
            n_v, n_t = len(me.vertices), len(me.loop_triangles)
            if not n_t:
                continue
            co = np.empty(n_v * 3, np.float32)
            me.vertices.foreach_get("co", co)
            idx = np.empty(n_t * 3, np.int32)
            me.loop_triangles.foreach_get("vertices", idx)
            mw = np.array(obj_eval.matrix_world, dtype=np.float64)
            world = co.reshape(n_v, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
            tri = world[idx.reshape(n_t, 3)]
            if np.linalg.det(mw[:3, :3]) < 0:
                tri = tri[:, ::-1, :]  # mirrored transforms flip winding
            tris.append(tri * factor)
            exported.append(object_id(obj) or obj.name)
        finally:
            obj_eval.to_mesh_clear()
    if not tris:
        raise ScriptError("nothing_to_export", "The revision has no visible mesh objects to export.")
    all_tris = np.concatenate(tris)
    normals = np.cross(all_tris[:, 1] - all_tris[:, 0], all_tris[:, 2] - all_tris[:, 0])
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lens, out=np.zeros_like(normals), where=lens > 0)
    record = np.zeros(len(all_tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")]))
    record["n"] = normals
    record["v"] = all_tris
    header = f"Sculpt Workspace STL units={units}".encode()[:80].ljust(80, b" ")
    with open(request["output"], "wb") as fh:
        fh.write(header)
        fh.write(struct.pack("<I", len(all_tris)))
        fh.write(record.tobytes())
    flat = all_tris.reshape(-1, 3)
    return {
        "triangles": len(all_tris),
        "objects": exported,
        "units": units,
        "bounds": {"min": flat.min(axis=0).tolist(), "max": flat.max(axis=0).tolist()},
    }


run(main)
