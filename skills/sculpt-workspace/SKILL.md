---
name: sculpt-workspace
description: Use when adding to or reviewing a sculpture through the Sculpt Workspace MCP tools (get_project, inspect_project, apply_edit, render_views, get_result, checkout_revision, export_model). Covers reading the brief, precise surface targeting, the sculpt edit helpers, dry runs, reviewing highlighted before/after renders, and fixing rejected edits.
---

<!-- Same text as src/sculpt_workspace/agent_guide.md (served as the sculpt_task MCP prompt). Keep them in sync. -->

# Sculpt Workspace playbook

You are adding to a sculpture in Blender through the Sculpt Workspace MCP tools. Geometry from the
original upload is protected and read-only; everything you add is yours to change later.

## 1. Understand the goal

1. `get_project(project_id)` gives the current revision id, the revision history (with what each
   edit changed and its `edit_job_id`), the latest render job, the brief and any running jobs.
   Without a project id it lists the projects.
2. `inspect_project(project_id)` returns the `brief`, the list of `references`, scene units, bounds
   and every object (with `protected: true` for the original) for the current revision.
3. If references exist, call it again with `include_references=true` to see the reference images.
4. Turn the brief into checkable targets before editing: what, where on the sculpture, how big
   (in Blender units *and* mm: `mm = units * scene.units.scale_length * 1000`, or `sculpt.mm()`
   in a script), what material, and from which views it must be visible.
5. If earlier revisions hold agent objects you need to change, read the script that made them:
   `get_result(edit_job_id, include_source=true, include_images=false)`.
6. Revisions have an `author`: `agent`, `user` (written by the person in the browser's code
   editor) or `upload`. Treat the user's revisions as intended: build on them and do not remove or
   undo their objects unless the brief asks you to.

## 2. Look before you build

- `get_result(latest_render.job_id, overlays=true)` draws a world grid with axis values,
  a scale bar (orthographic views), an axis gizmo and the view name on each image.
- `render_views(..., passes=["normal", "depth"])` shows surface orientation and depth when shading
  is ambiguous. In normal images, magenta means a face seen from behind (inverted normals).
- `get_result(..., crop={view, x, y, width, height})` zooms into a detail at full resolution.

## 3. Target precisely

Never guess coordinates from a picture.

- Point at a spot in an image: `inspect_project(query={"type": "image_ray", "artifact_id", "u", "v"})`
  returns the 3D position and normal of the surface under that pixel.
- Inside an edit script, `sculpt.ray(origin, direction)` and `sculpt.nearest(point)` hit the
  original sculpture directly and return `position`, `normal`, `distance`, `object_id`.
- Size things in the units the user asked for with `sculpt.mm(...)` / `sculpt.cm(...)`, or
  relative to `sculpt.scene_bounds()`.
- After an edit, check placement in numbers, not only by eye:
  `inspect_project(query={"type": "measure", "object_id": <your object>})` returns its size in mm,
  its closest distance to the sculpture and how many faces intersect it.

## 4. Edit in small steps

- One idea per edit, with a clear `summary`.
- Prefer the `sculpt` helpers over raw `bpy.ops` (operators need a UI context background Blender
  lacks): `from_curve`, `place_on_surface`, `conform_to`, `boolean`, `array_along`, `duplicate`,
  `material`, `assign_material`, `apply_modifiers`.
- Use `dry_run=true` for risky or large edits: the candidate is validated, checked and rendered,
  but no revision is created. `get_result` on the dry-run job shows the images. If it is right,
  publish it with `apply_edit(project_id, base_revision_id, from_dry_run=<dry-run edit_job_id>)`.
- Use `strict_checks=true` when the result will be 3D printed: geometry warnings then reject the
  edit (non-watertight mesh, inverted normals, floating parts, absurd size).
- `print()` diagnostic values (hit positions, sizes); output comes back in the result.

## 5. Review every edit

- Wait for results instead of polling: `get_result(job_id, wait_seconds=45)` returns as soon as the
  job (and the render an edit queues) is done; if `still_running` is true, call it again.
- `get_result(edit_job_id, passes=["color", "highlight"], compare=true)` shows each view before and
  after, plus a highlight pass: orange = new or changed in this edit, blue = your earlier objects,
  grey = the original.
- The automatic render adds `closeup_front` and `closeup_perspective`, framed on what changed.
- The edit result lists `changes` (added / modified / removed objects) and `checks` (geometry
  issues per changed object). Read both.
- Compare against the brief and references. If it is not right, fix it in a new edit; use
  `checkout_revision` to go back if an approach was wrong.

## 6. When an edit is rejected

- `protected_geometry_modified`: each violation names the object, what changed and `hints` for
  getting the same result without touching it (usually: build a new object, or
  `sculpt.duplicate(obj)` and change the copy; parent *your* object to the original, never the
  reverse).
- `geometry_check_failed`: fix the listed issues on your objects.
- `edit_script_error`: read `line`, `hint` and `traceback`.

## Worked example: a gold brooch on the figure's left chest

```python
lo, hi = sculpt.scene_bounds()
size = (hi - lo).length
# The figure's left is the viewer's right (+X) in the front view, which looks along +Y.
aim = Vector((lo.x + 0.62 * (hi.x - lo.x), 0.0, lo.z + 0.72 * (hi.z - lo.z)))
hit = sculpt.ray(Vector((aim.x, lo.y - size, aim.z)), Vector((0.0, 1.0, 0.0)))
assert hit, "missed the sculpture; adjust the aim point"
print("surface", hit["object_name"], tuple(hit["position"]), tuple(hit["normal"]))

r = sculpt.mm(15)  # a 30 mm brooch; use a fraction of `size` if the brief gives no size
ring = sculpt.from_curve(
    [Vector((r * math.cos(a), r * math.sin(a), 0.0)) for a in (i * math.tau / 24 for i in range(24))],
    radius=0.15 * r,
    name="BroochRing",
    cyclic=True,
)
bm = bmesh.new()
bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.45 * r)
gem_mesh = bpy.data.meshes.new("BroochGem")
bm.to_mesh(gem_mesh)
bm.free()
gem = sculpt.link(bpy.data.objects.new("BroochGem", gem_mesh))

sculpt.assign_material(ring, sculpt.material("Gold", color=(1.0, 0.77, 0.34), metallic=1.0, roughness=0.25))
sculpt.assign_material(gem, sculpt.material("Ruby", color=(0.6, 0.02, 0.05), roughness=0.1))
sculpt.place_on_surface(ring, hit["position"], hit["normal"], offset=0.15 * r)
sculpt.place_on_surface(gem, hit["position"], hit["normal"], offset=0.3 * r)
print("brooch at", tuple(ring.location), "radius", r)
```

Then review with `get_result(edit_job_id, wait_seconds=45, passes=["color", "highlight"], compare=true)`,
look at `closeup_front`, and confirm the fit with a `measure` query on the ring's object id (from the
edit result's `changes.added`).
