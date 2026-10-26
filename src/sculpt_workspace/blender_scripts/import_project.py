"""Import an upload: verify self-containment, assign stable object ids,
fingerprint the protection baseline, and save the working revision."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # type: ignore[import-not-found]
from _common import ScriptError, assign_ids, depsgraph, open_blend, run, save_blend
from scene_tools import fingerprint_all, inspect_scene, self_contained_issues


def main(request: dict) -> dict:
    open_blend(request["original"])
    issues = self_contained_issues()
    if issues:
        raise ScriptError(
            "not_self_contained",
            f"The file depends on {len(issues)} external {'item' if len(issues) == 1 else 'items'}. Pack all assets "
            "(File > External Data > Pack Resources) and make linked data local, then upload again.",
            {"issues": issues[:200]},
        )
    if not bpy.data.objects:
        raise ScriptError("empty_scene", "The file contains no objects.")
    assign_ids()
    dg = depsgraph()
    fingerprints = fingerprint_all(dg)
    unreliable = {oid: fp["unreliable"] for oid, fp in fingerprints.items() if fp["unreliable"]}
    if unreliable:
        raise ScriptError(
            "unsupported_geometry",
            "Some objects cannot be protected reliably, so edits could not be validated. "
            "Convert them to meshes or remove them, then upload again.",
            {"objects": [{"id": k, "name": fingerprints[k]["name"], "reasons": v} for k, v in unreliable.items()]},
        )
    inspection = inspect_scene(dg)
    save_blend(os.path.join(request["output_dir"], "model.blend"))
    return {"fingerprints": fingerprints, "inspection": inspection}


run(main)
