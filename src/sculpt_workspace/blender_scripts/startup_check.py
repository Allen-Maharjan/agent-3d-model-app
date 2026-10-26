"""Startup self-check: build a tiny scene and render it with Workbench."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bmesh  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]
from _common import ScriptError, run
from mathutils import Matrix  # type: ignore[import-not-found]
from sculpt_math import look_at


def main(request: dict) -> dict:
    scene = bpy.context.scene
    for obj in list(scene.collection.all_objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    me = bpy.data.meshes.new("check")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(me)
    bm.free()
    scene.collection.objects.link(bpy.data.objects.new("check", me))
    cam = bpy.data.objects.new("check_cam", bpy.data.cameras.new("check_cam"))
    scene.collection.objects.link(cam)
    cam.matrix_world = Matrix(look_at((3.0, -3.0, 2.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)))
    scene.camera = cam
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 32
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = request["output"]
    bpy.ops.render.render(write_still=True)
    if not os.path.isfile(request["output"]) or os.path.getsize(request["output"]) == 0:
        raise ScriptError("render_failed", "Workbench did not write the check image")
    return {"engine": "BLENDER_WORKBENCH"}


run(main)
