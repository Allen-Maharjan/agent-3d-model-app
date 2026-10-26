"""Run an authenticated agent edit script against a revision and save the
candidate. Validation happens afterwards in a separate, fresh process."""

import contextlib
import io
import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bmesh  # type: ignore[import-not-found]
import bpy  # type: ignore[import-not-found]
import mathutils  # type: ignore[import-not-found]
from _common import ID_PROP, ScriptError, assign_ids, depsgraph, object_id, open_blend, run, save_blend
from mathutils import Matrix, Vector  # type: ignore[import-not-found]
from world_mesh import SurfaceSet, load_meshes

OUTPUT_LIMIT = 20000
TRACK_AXES = ("X", "Y", "Z", "-X", "-Y", "-Z")


class _BoundedBuffer(io.StringIO):
    def write(self, s: str) -> int:
        if self.tell() < OUTPUT_LIMIT:
            super().write(s[: OUTPUT_LIMIT - self.tell()])
        return len(s)


class ProtectedObjectError(Exception):
    """Raised by helpers asked to change an object from the original upload."""


class SculptHelper:
    """Exposed to agent scripts as `sculpt`.

    Helpers never modify protected objects; they raise ProtectedObjectError
    instead, so a mistake fails fast with a clear message rather than after
    the full validation pass.
    """

    def __init__(self, protected_ids: list[str]):
        self.protected_ids = frozenset(protected_ids)
        self._sculpture = None

    # -- lookup ---------------------------------------------------------------

    def objects(self) -> dict:
        return {object_id(o): o for o in bpy.data.objects if object_id(o)}

    def get(self, oid: str):
        obj = self.objects().get(oid)
        if obj is None:
            raise KeyError(f"no object with id {oid!r}")
        return obj

    def id_of(self, obj):
        """The object's stable id, or None for objects created in this script."""
        return object_id(obj)

    def is_protected(self, obj_or_id) -> bool:
        oid = obj_or_id if isinstance(obj_or_id, str) else object_id(obj_or_id)
        return oid in self.protected_ids

    def _own(self, obj, action: str):
        if self.is_protected(obj):
            raise ProtectedObjectError(
                f"{action}: {obj.name!r} ({object_id(obj)}) is part of the original upload and cannot be changed. "
                "Build a new object, or duplicate it with sculpt.duplicate(obj) and change the copy."
            )
        return obj

    def link(self, obj, collection=None):
        """Link a new object into the scene (default: the scene collection)."""
        (collection or bpy.context.scene.collection).objects.link(obj)
        return obj

    def _ensure_linked(self, obj) -> None:
        if not obj.users_collection:
            self.link(obj)

    # -- units ----------------------------------------------------------------

    @property
    def scale_length(self) -> float:
        """Metres per Blender unit (scene unit scale); 1.0 when unset."""
        return float(bpy.context.scene.unit_settings.scale_length) or 1.0

    def mm(self, value):
        """Millimetres to Blender units, e.g. radius=sculpt.mm(15)."""
        return value / (1000.0 * self.scale_length)

    def cm(self, value):
        """Centimetres to Blender units."""
        return value / (100.0 * self.scale_length)

    def to_mm(self, units):
        """Blender units (a length, Vector or tuple) to millimetres."""
        factor = 1000.0 * self.scale_length
        if isinstance(units, (int, float)):
            return units * factor
        return tuple(c * factor for c in units)

    # -- surface queries ------------------------------------------------------

    def _surface(self, object_ids):
        if object_ids:
            return SurfaceSet(load_meshes(depsgraph(), object_ids))
        if self._sculpture is None:
            # Protected geometry never changes, so this BVH can be cached for the whole script.
            meshes = [m for m in load_meshes(depsgraph()) if m.id in self.protected_ids]
            self._sculpture = SurfaceSet(meshes)
        return self._sculpture

    def ray(self, origin, direction, object_ids=None, max_distance=None):
        """First surface hit along a ray, or None.

        Targets the original sculpture by default; pass object_ids to hit
        other objects (they must already have ids, i.e. exist in the base
        revision). Returns a dict with position, normal (mathutils.Vector),
        distance, object_id, object_name and face_index.
        """
        return self._surface(object_ids).ray(origin, direction, max_distance)

    def nearest(self, point, object_ids=None, max_distance=None):
        """Closest surface point to `point`, or None. Same targets and result as ray()."""
        return self._surface(object_ids).nearest(point, max_distance)

    def scene_bounds(self):
        """(min, max) world-space corners of the original sculpture's visible geometry."""
        s = self._surface(None)
        if s.bounds_min is None:
            raise ValueError("the original upload has no visible surface geometry")
        return Vector(s.bounds_min.tolist()), Vector(s.bounds_max.tolist())

    # -- placement ------------------------------------------------------------

    def place_on_surface(self, obj, position, normal, offset=0.0, axis="Z", up="Y", spin=0.0):
        """Move `obj` to `position` and point its local `axis` along `normal`.

        `offset` lifts the object along the normal (negative sinks it in).
        `spin` rotates it around the normal in radians. Scale is kept.
        """
        self._own(obj, "place_on_surface")
        if axis not in TRACK_AXES:
            raise ValueError(f"axis must be one of {TRACK_AXES}")
        n = Vector(normal).normalized()
        if axis.lstrip("-") == up:
            up = "X" if up != "X" else "Y"
        rot = n.to_track_quat(axis, up).to_matrix().to_4x4()
        if spin:
            rot = Matrix.Rotation(spin, 4, n) @ rot
        scale = obj.matrix_world.to_scale()
        obj.matrix_world = Matrix.Translation(Vector(position) + n * offset) @ rot @ Matrix.Diagonal((*scale, 1.0))
        return obj

    def duplicate(self, obj, linked=False, name=None):
        """Copy an object (protected or not) into a new, unprotected object.

        Mesh data is copied too unless `linked=True`; sharing data with a
        protected object would change it, so that is refused.
        """
        if linked and self.is_protected(obj):
            raise ProtectedObjectError(
                "duplicate(linked=True) would share mesh data with a protected object; use linked=False."
            )
        dup = obj.copy()
        if obj.data is not None and not linked:
            dup.data = obj.data.copy()
        if ID_PROP in dup:
            del dup[ID_PROP]
        if name:
            dup.name = name
        for coll in obj.users_collection or [bpy.context.scene.collection]:
            coll.objects.link(dup)
        return dup

    def array_along(self, obj, positions, normals=None, offset=0.0, linked=False, axis="Z"):
        """Place a copy of `obj` at each position (aligned to `normals` if given).

        Returns the list of new objects; the source object is left where it is.
        """
        if normals is not None and len(normals) != len(positions):
            raise ValueError("normals must have the same length as positions")
        out = []
        for i, pos in enumerate(positions):
            dup = self.duplicate(obj, linked=linked)
            if normals is not None:
                self.place_on_surface(dup, pos, normals[i], offset=offset, axis=axis)
            else:
                dup.location = Vector(pos)
            out.append(dup)
        return out

    # -- construction ---------------------------------------------------------

    def from_curve(
        self,
        points,
        radius=0.01,
        name="Curve",
        cyclic=False,
        smooth=True,
        resolution=12,
        bevel_resolution=4,
        to_mesh=True,
    ):
        """A tube through world-space `points` (a wire, rim, strap or chain path).

        Returns a mesh object (or the curve object when to_mesh=False).
        """
        if len(points) < 2:
            raise ValueError("from_curve needs at least two points")
        cu = bpy.data.curves.new(name, "CURVE")
        cu.dimensions = "3D"
        cu.resolution_u = resolution
        cu.bevel_depth = radius
        cu.bevel_resolution = bevel_resolution
        cu.use_fill_caps = True
        if smooth:
            sp = cu.splines.new("BEZIER")
            sp.bezier_points.add(len(points) - 1)
            for bp, p in zip(sp.bezier_points, points, strict=True):
                bp.co = Vector(p)
                bp.handle_left_type = bp.handle_right_type = "AUTO"
        else:
            sp = cu.splines.new("POLY")
            sp.points.add(len(points) - 1)
            for pt, p in zip(sp.points, points, strict=True):
                pt.co = (*Vector(p), 1.0)
        sp.use_cyclic_u = cyclic
        obj = bpy.data.objects.new(name, cu)
        self.link(obj)
        return self.apply_modifiers(obj) if to_mesh else obj

    def apply_modifiers(self, obj):
        """Bake modifiers (and curve/text geometry) into a plain mesh.

        Returns the object to use from now on: a curve becomes a new mesh
        object that keeps the curve's id, name, transform and collections.
        """
        self._own(obj, "apply_modifiers")
        self._ensure_linked(obj)
        dg = depsgraph()
        me = bpy.data.meshes.new_from_object(obj.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
        if obj.type == "MESH":
            old = obj.data
            obj.modifiers.clear()
            obj.data = me
            if old.users == 0:
                bpy.data.meshes.remove(old)
            return obj
        new = bpy.data.objects.new(obj.name, me)
        new.matrix_world = obj.matrix_world.copy()
        for slot in obj.material_slots:
            if slot.material is not None and slot.material.name not in me.materials:
                me.materials.append(slot.material)
        for coll in obj.users_collection:
            coll.objects.link(new)
        oid = object_id(obj)
        name = obj.name
        bpy.data.objects.remove(obj, do_unlink=True)
        if oid:
            new[ID_PROP] = oid
        new.name = name
        return new

    def conform_to(self, obj, target_id, offset=0.0, method="NEAREST_SURFACEPOINT", apply=True):
        """Shrinkwrap `obj` onto the surface of object `target_id`.

        method: NEAREST_SURFACEPOINT (default), PROJECT, NEAREST_VERTEX or
        TARGET_PROJECT. The target is only read, so it may be protected.
        """
        self._own(obj, "conform_to")
        self._ensure_linked(obj)
        mod = obj.modifiers.new("SculptConform", "SHRINKWRAP")
        mod.target = self.get(target_id)
        mod.wrap_method = method
        mod.offset = offset
        if method == "PROJECT":
            mod.use_negative_direction = True
            mod.use_positive_direction = True
        return self.apply_modifiers(obj) if apply else obj

    def boolean(self, obj, cutter, operation="UNION", apply=True, remove_cutter=True):
        """Boolean `cutter` into `obj` (UNION, DIFFERENCE or INTERSECT).

        `obj` must be yours; `cutter` may be protected (it is only read and
        never removed). Uses the exact solver.
        """
        self._own(obj, "boolean")
        self._ensure_linked(obj)
        self._ensure_linked(cutter)
        mod = obj.modifiers.new("SculptBoolean", "BOOLEAN")
        mod.operation = operation
        mod.object = cutter
        with contextlib.suppress(TypeError, AttributeError):
            mod.solver = "EXACT"
        if not apply:
            return obj
        result = self.apply_modifiers(obj)
        if remove_cutter and not self.is_protected(cutter):
            bpy.data.objects.remove(cutter, do_unlink=True)
        return result

    # -- materials ------------------------------------------------------------

    def material(self, name, color=(0.8, 0.8, 0.8), metallic=0.0, roughness=0.5, emission=None, emission_strength=1.0):
        """A Principled BSDF material. `color` and `emission` are RGB in 0..1.

        Also sets the viewport colour, which is what Workbench renders show.
        """
        mat = bpy.data.materials.new(name)
        rgba = (*tuple(color)[:3], 1.0)
        mat.diffuse_color = rgba
        mat.metallic = metallic
        mat.roughness = roughness
        with contextlib.suppress(AttributeError):
            mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if mat.node_tree else None
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = rgba
            bsdf.inputs["Metallic"].default_value = metallic
            bsdf.inputs["Roughness"].default_value = roughness
            if emission is not None:
                bsdf.inputs["Emission Color"].default_value = (*tuple(emission)[:3], 1.0)
                bsdf.inputs["Emission Strength"].default_value = emission_strength
        return mat

    def assign_material(self, obj, material):
        """Replace the first material slot of `obj` (or add one)."""
        self._own(obj, "assign_material")
        mats = obj.data.materials
        if len(mats):
            mats[0] = material
        else:
            mats.append(material)
        return obj


HINTS = (
    (
        ("context is incorrect", "poll() failed", "context.active_object", "context.selected"),
        "bpy.ops operators need a UI context that background Blender does not have. Use bpy.data, bmesh, or "
        "the sculpt helpers (from_curve, boolean, conform_to, apply_modifiers) instead.",
    ),
    (("no object with id",), "List valid ids with sculpt.objects() or inspect_project."),
    (
        ("is not defined",),
        "Only bpy, bmesh, mathutils, Vector, Matrix, math and sculpt are predefined; import anything else.",
    ),
)


def hint_for(exc: BaseException) -> str | None:
    if isinstance(exc, ProtectedObjectError):
        return "Protected objects are read-only. Change your own objects, or copy with sculpt.duplicate(obj)."
    text = str(exc)
    for needles, hint in HINTS:
        if any(n in text for n in needles):
            return hint
    return None


def main(request: dict) -> dict:
    open_blend(request["base"])
    previous = {object_id(o): o.name for o in bpy.data.objects if object_id(o)}
    namespace = {
        "__name__": "__agent_edit__",
        "bpy": bpy,
        "bmesh": bmesh,
        "mathutils": mathutils,
        "Vector": Vector,
        "Matrix": Matrix,
        "math": math,
        "sculpt": SculptHelper(request.get("protected_ids", [])),
    }
    output = _BoundedBuffer()
    try:
        code = compile(request["source"], "<agent_edit>", "exec")
        with contextlib.redirect_stdout(output):
            exec(code, namespace)
    except SyntaxError as exc:
        raise ScriptError(
            "edit_script_error",
            f"SyntaxError: {exc.msg} (line {exc.lineno})",
            {"line": exc.lineno, "output": output.getvalue()},
        ) from exc
    except SystemExit as exc:
        raise ScriptError(
            "edit_script_error",
            f"The edit script called sys.exit({exc.code!r}); scripts must finish normally.",
            {"output": output.getvalue()},
        ) from exc
    except Exception as exc:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "<agent_edit>"]
        raise ScriptError(
            "edit_script_error",
            f"{type(exc).__name__}: {exc}",
            {
                "line": frames[-1].lineno if frames else None,
                "hint": hint_for(exc),
                "traceback": "".join(traceback.format_exception(exc))[-4000:],
                "output": output.getvalue(),
            },
        ) from exc
    with contextlib.suppress(Exception):
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    ids = assign_ids(previous)
    save_blend(request["output"])
    return {"created_ids": ids["created"], "output": output.getvalue()}


run(main)
