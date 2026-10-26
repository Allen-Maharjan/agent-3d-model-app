"""Validate a candidate revision in a fresh process: re-check external
dependencies, fingerprint every object for comparison with the baseline, and
(for edits) sanity-check the agent's own geometry."""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import depsgraph, open_blend, run
from scene_tools import fingerprint_all, inspect_scene, self_contained_issues


def main(request: dict) -> dict:
    open_blend(request["candidate"])
    issues = self_contained_issues()
    dg = depsgraph()
    out = {"issues": issues, "fingerprints": fingerprint_all(dg), "inspection": inspect_scene(dg)}
    protected = request.get("protected_ids")
    if protected is not None:
        try:
            from geometry_checks import check_objects

            out["checks"] = check_objects(dg, set(protected))
        except Exception as exc:  # noqa: BLE001 - checks are advisory; never block validation
            out["checks_error"] = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
    return out


run(main)
