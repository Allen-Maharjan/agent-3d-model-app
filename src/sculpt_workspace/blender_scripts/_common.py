"""Shared helpers for scripts executed inside Blender.

Protocol: Blender is started with ``-- <request.json> <result.json>``. A script
calls ``run(main)``; ``main(request)`` returns a dict that is written with
``ok: true``, or raises ``ScriptError`` which is written as ``ok: false``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from collections.abc import Callable
from typing import Any

import bpy  # type: ignore[import-not-found]

ID_PROP = "sculpt_id"


class ScriptError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _argv() -> list[str]:
    argv = sys.argv
    return argv[argv.index("--") + 1 :] if "--" in argv else []


def run(main: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    args = _argv()
    if len(args) != 2:
        print("expected: -- <request.json> <result.json>", file=sys.stderr)
        sys.exit(2)
    request_path, result_path = args
    with open(request_path, encoding="utf-8") as fh:
        request = json.load(fh)
    try:
        out: dict[str, Any] = {"ok": True, **main(request)}
    except ScriptError as exc:
        out = {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
    except Exception as exc:  # noqa: BLE001 - every failure must reach the result file
        traceback.print_exc()
        out = {
            "ok": False,
            "error": {
                "code": "blender_script_error",
                "message": f"{type(exc).__name__}: {exc}",
                "details": {"traceback": traceback.format_exc()[-4000:]},
            },
        }
    out["blender_version"] = bpy.app.version_string
    tmp = result_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, result_path)


def open_blend(path: str) -> None:
    """Open a .blend without trusting its embedded scripts."""
    if not os.path.isfile(path):
        raise ScriptError("file_missing", f"File not found: {os.path.basename(path)}")
    try:
        try:
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
        except TypeError:
            bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
    except Exception as exc:
        raise ScriptError(
            "invalid_blend_file",
            "Blender could not open this file. It may be corrupt, truncated, or saved by a newer Blender.",
            {"blender_message": str(exc)[:1000]},
        ) from exc
    if os.path.abspath(bpy.data.filepath) != os.path.abspath(path):
        raise ScriptError("invalid_blend_file", "Blender could not open this file.")


def save_blend(path: str) -> None:
    """Save the current file; packed data stays packed, nothing is remapped."""
    bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False, compress=True, relative_remap=False)


def object_id(obj: Any) -> str | None:
    value = obj.get(ID_PROP)
    return str(value) if isinstance(value, str) and value else None


def new_id() -> str:
    return "obj_" + uuid.uuid4().hex[:16]


def assign_ids(previous_names: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Ensure every object has a unique stable id.

    Duplicated objects inherit their source's custom property; when an id is
    shared, the object still carrying the name it had before keeps the id and
    the copies receive fresh ids.
    """
    previous_names = previous_names or {}
    by_id: dict[str, list[Any]] = {}
    created: list[str] = []
    for obj in bpy.data.objects:
        oid = object_id(obj)
        if oid is None:
            oid = new_id()
            obj[ID_PROP] = oid
            created.append(oid)
        by_id.setdefault(oid, []).append(obj)
    for oid, objs in by_id.items():
        if len(objs) < 2:
            continue
        keep_name = previous_names.get(oid)
        keeper = next((o for o in objs if o.name == keep_name), objs[0])
        for obj in objs:
            if obj is not keeper:
                fresh = new_id()
                obj[ID_PROP] = fresh
                created.append(fresh)
    return {"created": created}


def depsgraph() -> Any:
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    return dg
