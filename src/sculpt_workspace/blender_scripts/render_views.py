"""Render named views, explicit cameras and close-ups of a revision.

Besides the normal colour image, a render can produce analysis passes:

* ``highlight``: Workbench with flat object colours. Objects new or changed
  in this revision are orange, older agent objects blue, the original grey.
* ``normal``: world-space surface normals as RGB (``(n + 1) / 2``); faces seen
  from behind (inverted normals) are magenta.
* ``depth``: distance from the camera, near = bright, background = black.

Normal and depth are ray cast against evaluated geometry at a bounded
analysis resolution and upscaled, so they never depend on the render engine.

Temporary cameras, lights, colours and world settings exist only in this
process; the revision file is never saved here, so they cannot enter a saved
model.
"""

import math
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # type: ignore[import-not-found]
import numpy as np  # type: ignore[import-not-found]
from _common import ScriptError, depsgraph, object_id, open_blend, run
from mathutils import Matrix  # type: ignore[import-not-found]
from scene_tools import GEOMETRY_TYPES
from sculpt_math import custom_camera, standard_camera
from world_mesh import SurfaceSet, load_meshes

NEUTRAL_BACKGROUND = (0.23, 0.235, 0.24)
HIGHLIGHT_NEW = (1.0, 0.42, 0.05, 1.0)
HIGHLIGHT_AGENT = (0.22, 0.5, 0.95, 1.0)
HIGHLIGHT_ORIGINAL = (0.62, 0.62, 0.62, 1.0)
BACKFACE_RGB = (255, 0, 255)
BACKGROUND_RGB = tuple(round(c * 255) for c in NEUTRAL_BACKGROUND)
ANALYSIS_MAX = 256
COLOR_PASS = "color"  # noqa: S105 - a render pass name, not a secret
CLOSEUP_MARGIN = 1.15


def visible_bounds(dg, object_ids=None) -> tuple[list[float], list[float]]:
    """World bounds of visible geometry, or of the given objects (visible or not)."""
    wanted = set(object_ids) if object_ids else None
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for obj in bpy.data.objects:
        if obj.type not in GEOMETRY_TYPES:
            continue
        if wanted is not None:
            if object_id(obj) not in wanted:
                continue
        elif not obj.visible_get() or obj.hide_render:
            continue
        obj_eval = obj.evaluated_get(dg)
        try:
            me = obj_eval.to_mesh()
        except RuntimeError:
            continue
        try:
            n = len(me.vertices) if me else 0
            if not n:
                continue
            co = np.empty(n * 3, np.float32)
            me.vertices.foreach_get("co", co)
            mw = np.array(obj_eval.matrix_world, dtype=np.float64)
            world = co.reshape(n, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
            lo = np.minimum(lo, world.min(axis=0))
            hi = np.maximum(hi, world.max(axis=0))
        finally:
            obj_eval.to_mesh_clear()
    if not np.all(np.isfinite(lo)):
        if wanted is not None:
            raise ScriptError(
                "unknown_objects", "None of the close-up objects has geometry.", {"object_ids": sorted(wanted)}
            )
        raise ScriptError("no_visible_geometry", "The revision has no visible geometry to render.")
    return lo.tolist(), hi.tolist()


def engine_id(name: str) -> str:
    items = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items.keys()
    if name == "workbench":
        return "BLENDER_WORKBENCH"
    for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        if candidate in items:
            return candidate
    raise ScriptError("engine_unavailable", "Eevee is not available in this Blender build.")


def setup_scene(engine: str, resolution: int) -> None:
    scene = bpy.context.scene
    render = scene.render
    render.engine = engine_id(engine)
    render.resolution_x = render.resolution_y = resolution
    render.resolution_percentage = 100
    render.film_transparent = False
    render.use_compositing = False
    render.use_sequencer = False
    render.image_settings.file_format = "PNG"
    render.image_settings.color_mode = "RGB"
    render.image_settings.color_depth = "8"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    world = bpy.data.worlds.new("SculptRenderWorld")
    world.color = NEUTRAL_BACKGROUND
    scene.world = world
    if engine == "workbench":
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "MATERIAL"
        shading.show_cavity = True
        shading.cavity_type = "WORLD"
        shading.show_shadows = False
        shading.show_specular_highlight = True
    else:
        world.use_nodes = True
        bg = world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs[0].default_value = (*NEUTRAL_BACKGROUND, 1.0)
            bg.inputs[1].default_value = 1.0


def add_key_light(camera_cfg: dict) -> object:
    light = bpy.data.objects.new("SculptRenderKey", bpy.data.lights.new("SculptRenderKey", "SUN"))
    light.data.energy = 3.0
    bpy.context.scene.collection.objects.link(light)
    light.matrix_world = Matrix(camera_cfg["matrix_world"])
    return light


def image_name(camera_name: str, pass_name: str) -> str:
    """`front` for the colour image, `front--highlight` for a pass."""
    return camera_name if pass_name == COLOR_PASS else f"{camera_name}--{pass_name}"


def setup_highlight(protected: set, changed: set) -> None:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "OBJECT"
    shading.show_cavity = True
    shading.cavity_type = "WORLD"
    shading.show_shadows = False
    shading.show_specular_highlight = False
    for obj in bpy.data.objects:
        oid = object_id(obj)
        if oid in changed:
            obj.color = HIGHLIGHT_NEW
        elif oid is None or oid in protected:
            obj.color = HIGHLIGHT_ORIGINAL
        else:
            obj.color = HIGHLIGHT_AGENT


def write_png(path: str, rgb: np.ndarray) -> None:
    """Minimal 8-bit RGB PNG encoder, so analysis passes need no image datablocks."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in np.ascontiguousarray(rgb, dtype=np.uint8))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 6)))
        fh.write(chunk(b"IEND", b""))


def cast_pixels(surface: SurfaceSet, cfg: dict, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-pixel world normal, hit distance (NaN on a miss) and back-facing flag at size x size."""
    mw = np.array(cfg["matrix_world"], dtype=np.float64)
    proj = np.array(cfg["projection"], dtype=np.float64)
    inv = np.linalg.inv(proj @ np.linalg.inv(mw))
    centers = (np.arange(size) + 0.5) / size
    u, v = np.meshgrid(centers, centers)
    x, y = 2.0 * u - 1.0, 1.0 - 2.0 * v
    ones = np.ones_like(x)

    def unproject(z: float) -> np.ndarray:
        clip = np.stack([x, y, ones * z, ones], axis=-1) @ inv.T
        return clip[..., :3] / clip[..., 3:4]

    near, far = unproject(-1.0), unproject(1.0)
    dirs = far - near
    lengths = np.linalg.norm(dirs, axis=-1)
    dirs = dirs / lengths[..., None]
    normal = np.zeros((size, size, 3))
    dist = np.full((size, size), np.nan)
    back = np.zeros((size, size), dtype=bool)
    for i in range(size):
        for j in range(size):
            hit = surface.ray(near[i, j].tolist(), dirs[i, j].tolist(), float(lengths[i, j]))
            if hit is None:
                continue
            n = np.array(hit["normal"])
            normal[i, j] = n
            dist[i, j] = hit["distance"]
            back[i, j] = float(n @ dirs[i, j]) > 0.0
    return normal, dist, back


def analysis_images(surface: SurfaceSet, cfg: dict, resolution: int, passes: list) -> dict:
    size = min(resolution, ANALYSIS_MAX)
    normal, dist, back = cast_pixels(surface, cfg, size)
    hit = ~np.isnan(dist)
    idx = (np.arange(resolution) * size) // resolution
    out = {}
    if "normal" in passes:
        img = np.empty((size, size, 3), dtype=np.uint8)
        img[:] = BACKGROUND_RGB
        img[hit] = np.clip((normal[hit] + 1.0) * 127.5, 0, 255).astype(np.uint8)
        img[hit & back] = BACKFACE_RGB
        out["normal"] = (img[idx][:, idx], {})
    if "depth" in passes:
        img = np.zeros((size, size, 3), dtype=np.uint8)
        meta = {"depth_range": None}
        if hit.any():
            lo, hi = float(np.nanmin(dist)), float(np.nanmax(dist))
            span = max(hi - lo, 1e-12)
            shade = 255.0 - 200.0 * (dist[hit] - lo) / span
            img[hit] = np.repeat(shade[:, None], 3, axis=1).astype(np.uint8)
            meta = {"depth_range": {"near": round(lo, 7), "far": round(hi, 7), "near_value": 255, "far_value": 55}}
        out["depth"] = (img[idx][:, idx], meta)
    return out


def main(request: dict) -> dict:
    open_blend(request["model"])
    dg = depsgraph()
    lo, hi = visible_bounds(dg)
    center = [(a + b) / 2.0 for a, b in zip(lo, hi, strict=True)]
    radius = max(math.dist(lo, hi) / 2.0, 1e-4)
    frame = request["frame"]
    resolution = int(request["resolution"])
    engine = request.get("engine", "workbench")
    passes = request.get("passes") or ["color"]
    setup_scene(engine, resolution)

    configs = [
        standard_camera(name, frame["front"], frame["up"], center, radius, resolution) for name in request["views"]
    ]
    configs += [custom_camera(spec, frame["up"], radius, resolution) for spec in request.get("cameras", [])]
    closeups = request.get("closeups")
    focus = None
    if closeups and closeups.get("object_ids"):
        flo, fhi = visible_bounds(dg, closeups["object_ids"])
        fcenter = [(a + b) / 2.0 for a, b in zip(flo, fhi, strict=True)]
        fradius = max(math.dist(flo, fhi) / 2.0 * CLOSEUP_MARGIN, radius * 0.02, 1e-4)
        focus = {"object_ids": closeups["object_ids"], "center": fcenter, "radius": fradius}
        for name in closeups.get("views") or ["front", "perspective"]:
            cfg = standard_camera(name, frame["front"], frame["up"], fcenter, fradius, resolution)
            cfg["name"] = f"closeup_{name}"
            configs.append(cfg)

    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("SculptRenderCamera")
    cam = bpy.data.objects.new("SculptRenderCamera", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    key = None

    def aim(cfg: dict) -> None:
        ortho = cfg["type"] == "orthographic"
        cam_data.type = "ORTHO" if ortho else "PERSP"
        cam_data.sensor_fit = "AUTO"
        cam_data.sensor_width = cfg["sensor_mm"]
        cam_data.lens = cfg["lens_mm"]
        if ortho:
            cam_data.ortho_scale = cfg["ortho_scale"]
        cam_data.clip_start = cfg["clip_start"]
        cam_data.clip_end = cfg["clip_end"]
        cam_data.shift_x = cam_data.shift_y = 0.0
        cam.matrix_world = Matrix(cfg["matrix_world"])
        dg.update()
        projection = cam.calc_matrix_camera(dg, x=resolution, y=resolution, scale_x=1.0, scale_y=1.0)
        cfg["projection"] = [list(row) for row in projection]
        cfg["matrix_world"] = [list(row) for row in cam.matrix_world]

    def shoot(cfg: dict, pass_name: str) -> str:
        name = image_name(cfg["name"], pass_name)
        path = os.path.join(request["output_dir"], f"{name}.png")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        if not os.path.isfile(path):
            raise ScriptError("render_failed", f"Blender did not write the {name} view.")
        return path

    outputs = []

    def record(cfg: dict, pass_name: str, path: str, extra: dict | None = None) -> None:
        camera = dict(cfg)
        outputs.append(
            {
                "view": image_name(cfg["name"], pass_name),
                "base_view": cfg["name"],
                "pass": pass_name,
                "file": os.path.basename(path),
                "camera": camera,
                **(extra or {}),
            }
        )

    for cfg in configs:
        aim(cfg)
        if engine == "eevee":
            if key is not None:
                bpy.data.objects.remove(key, do_unlink=True)
            key = add_key_light(cfg)
            dg.update()
        record(cfg, "color", shoot(cfg, "color"))

    if "highlight" in passes:
        if key is not None:
            bpy.data.objects.remove(key, do_unlink=True)
            key = None
        setup_highlight(set(request.get("protected_ids") or []), set(request.get("changed_ids") or []))
        for cfg in configs:
            aim(cfg)
            record(cfg, "highlight", shoot(cfg, "highlight"))

    analysis = [p for p in passes if p in ("normal", "depth")]
    if analysis:
        surface = SurfaceSet(load_meshes(dg, include_unidentified=True))
        for cfg in configs:
            for pass_name, (img, extra) in analysis_images(surface, cfg, resolution, analysis).items():
                path = os.path.join(request["output_dir"], f"{image_name(cfg['name'], pass_name)}.png")
                write_png(path, img)
                record(cfg, pass_name, path, extra)

    return {
        "views": outputs,
        "bounds": {"min": lo, "max": hi, "center": center, "radius": radius},
        "focus": focus,
        "frame": frame,
        "engine": engine_id(engine),
        "resolution": resolution,
        "passes": passes,
    }


run(main)
