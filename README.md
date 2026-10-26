# Sculpt Workspace

A local app where AI agents iterate on a sculpture in Blender (inspect → edit → render → review) while the geometry from your original upload stays protected. The browser and eight MCP tools (plus a `sculpt_task` playbook prompt) call the same application services.

- Browser: `http://localhost:8765`
- MCP (Streamable HTTP, bearer token): `http://127.0.0.1:8765/mcp`

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Blender 5.2 LTS. It is discovered automatically on macOS (`/Applications/Blender.app`, `~/Applications`) and Linux (`PATH`, `/usr/bin`, `/snap/bin`, `/opt/blender*`). Override with `SCULPT_BLENDER_PATH`.

## Run

```sh
uv sync
uv run python -m sculpt_workspace
```

At startup the server checks the Blender version and renders a tiny Workbench image. If Blender is missing or fails, the UI still opens and shows "Blender unavailable" with the reason; Blender-backed actions return `blender_unavailable`.

The MCP token is generated into `<data dir>/mcp_token` (mode 0600) unless `SCULPT_MCP_TOKEN` is set. The server logs the file path, never the token.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SCULPT_DATA_DIR` | macOS `~/Library/Application Support/SculptWorkspace`, Linux `$XDG_DATA_HOME/sculpt-workspace` | Database, revisions, renders, logs |
| `SCULPT_PORT` | `8765` | Port (always bound to loopback) |
| `SCULPT_HOST` | `127.0.0.1` | Must be a loopback address |
| `SCULPT_BLENDER_PATH` | auto | Blender executable |
| `SCULPT_MCP_TOKEN` | generated file | MCP bearer token |
| `SCULPT_MAX_UPLOAD_MB` | `512` | Upload limit |
| `SCULPT_DEADLINE_{IMPORT,EDIT,VALIDATE,RENDER,INSPECT,EXPORT,STARTUP}` | 900/600/900/900/300/600/120 s | Blender job deadlines |
| `SCULPT_ALLOW_ANY_BLENDER_VERSION` | `0` | Allow versions other than 5.2 |
| `SCULPT_SKIP_BLENDER_CHECK` | `0` | Skip the startup render |

## Connect an agent

Export the token in the shell that launches the agent:

```sh
export SCULPT_MCP_TOKEN="$(cat "$HOME/Library/Application Support/SculptWorkspace/mcp_token")"   # macOS
export SCULPT_MCP_TOKEN="$(cat "${XDG_DATA_HOME:-$HOME/.local/share}/sculpt-workspace/mcp_token")" # Linux
```

### Claude Code

```sh
claude mcp add --transport http sculpt-workspace http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer ${SCULPT_MCP_TOKEN}"
```

Or in a project `.mcp.json`, which expands environment variables so the token stays out of the file:

```json
{
  "mcpServers": {
    "sculpt-workspace": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${SCULPT_MCP_TOKEN}" }
    }
  }
}
```

Docs: <https://code.claude.com/docs/en/mcp>

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.sculpt-workspace]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "SCULPT_MCP_TOKEN"
```

Docs: <https://learn.chatgpt.com/docs/extend/mcp#streamable-http-servers>

### Agent playbook

The server ships a playbook covering how to read the brief, target surfaces precisely, edit in small reviewed steps and fix rejections, with a worked example. Agents can get it in two ways:

- **MCP tool** `get_playbook()`. This works in every client, including Codex, which does not use MCP prompts.
- **MCP prompt** `sculpt_task(project_id, goal)`. In Claude Code it appears as `/mcp__sculpt-workspace__sculpt_task`.
- **Claude Code skill**: copy `skills/sculpt-workspace/` to `~/.claude/skills/` (or to a project's `.claude/skills/`).

`skills/sculpt-workspace/SKILL.md` and `src/sculpt_workspace/agent_guide.md` hold the same text; keep them in sync.

### Brief and reference images

On the project page, write a **brief** (what to add, where, how big, which material) and add up to 8 **reference images** with notes. `inspect_project` returns the brief and the reference list; `include_references=true` attaches the images. A specific brief gives the agent a goal it can check its renders against.

## MCP tools

| Tool | Contract |
|---|---|
| `get_project` | Start here. With `project_id`: current revision id, revision history (summary, `edit_job_id`, objects added/modified/removed), latest render job id and views, brief, references, active jobs. Without it: the project list. |
| `get_playbook` | The agent playbook as text. |
| `inspect_project` | Inspect a revision (default: current). Without `query`, returns the brief, reference list and cached scene data at once (`include_references` attaches the images). With a bounded spatial query (`region`, `nearest`, `surface_samples`, `ray`, `image_ray`, `measure`) returns a job id. `measure` gives an object's size in mm, its closest distance to the sculpture (or `target_ids`) and intersecting face pairs. |
| `apply_edit` | Project, base revision (must be current), Python source, summary. Validates preservation and runs geometry checks in a fresh Blender process, publishes a new revision, then renders it with a highlight pass and close-ups of what changed. Returns `edit_job_id` and `render_job_id`. `dry_run` validates and renders without publishing; `from_dry_run` publishes a succeeded dry run's script (re-run and re-validated on the same base); `strict_checks` rejects geometry warnings. |
| `render_views` | Named views (`front`, `right`, `back`, `left`, `top`, `perspective`) or explicit cameras; Workbench by default, `eevee` on request; optional `frame` with `persist_frame`; `focus_object_ids` for close-ups; extra `passes` (`highlight`, `normal`, `depth`). |
| `get_result` | Job state, reports, revision and artifact ids. Render results include PNG image content blocks (the colour image of each rendered view at 512 px by default, specific `views` and `passes`, or a `crop`). `overlays` adds a grid, scale bar and axis gizmo; `compare` shows each view before and after. `wait_seconds` (max 50) waits for the job and an edit's render to finish; `include_source` returns an edit's script. Results are compact by default (`detail="full"` adds camera matrices and bounds). |
| `checkout_revision` | Make an existing revision current, guarded by `expected_current_revision_id`. History is never deleted. |
| `export_model` | `.blend` (returned directly) or STL (job) for an explicit revision. |

### Edit scripts

Scripts run inside Blender with `bpy`, `mathutils`, and a `sculpt` helper:

```python
brooch = bpy.data.objects.new("Brooch", bpy.data.meshes.new("Brooch"))
# ... build geometry ...
sculpt.link(brooch)
print(sculpt.protected_ids)   # ids of objects from the original upload
```

The script namespace has `bpy`, `bmesh`, `mathutils`, `Vector`, `Matrix`, `math` and `sculpt`. `print()` output comes back in the result. Helpers on `sculpt`:

| Helper | Purpose |
|---|---|
| `objects()`, `get(id)`, `id_of(obj)`, `is_protected(obj_or_id)`, `protected_ids`, `link(obj)` | Look up objects and their stable ids |
| `scene_bounds()` | World bounds of the original sculpture |
| `mm(x)`, `cm(x)`, `to_mm(units)`, `scale_length` | Convert between real-world sizes and Blender units using the scene's unit scale |
| `ray(origin, direction)`, `nearest(point)` | Surface hits on the original (or `object_ids`): position, normal, distance, object and face |
| `place_on_surface(obj, position, normal, offset=0, axis="Z", spin=0)` | Put an object on a surface, aligned to its normal |
| `duplicate(obj)`, `array_along(obj, positions, normals=None)` | Unprotected copies, singly or along points |
| `from_curve(points, radius, cyclic=False)` | A tube mesh through points (wires, rims, straps) |
| `conform_to(obj, target_id, offset=0)` | Shrinkwrap onto a surface (the target is only read) |
| `boolean(obj, cutter, operation="UNION")` | Exact boolean; the cutter may be protected and is then never removed |
| `material(name, color, metallic, roughness, emission)`, `assign_material(obj, mat)` | Principled materials that also show in Workbench |
| `apply_modifiers(obj)` | Bake modifiers/curves into a mesh; returns the object to use |

Helpers refuse to change protected objects. Failed scripts return the line, a traceback and, for common mistakes (such as `bpy.ops` needing a UI context), a hint.

Authenticated scripts are trusted local code: Blender runs with factory settings, auto-exec disabled, a private working directory, and a minimal environment, which is operational isolation and not a security sandbox.

### Geometry checks

After protection passes, objects the edit added or modified are checked: `not_evaluable` and `non_finite` are errors and always reject the edit; `empty`, `too_large`, `too_small`, `non_manifold`, `inverted_normals` and `floating` are warnings and reject only with `strict_checks`; `hidden` and `intersects_original` are informational. Results are in the edit result's `checks` and the revision's protection report.

### Review images

- **Highlight pass** (`<view>--highlight`): orange = added or changed in this revision, blue = earlier agent objects, grey = the original. Edit renders include it automatically, and the browser's *Changes* toggle shows it.
- **Close-ups** (`closeup_front`, `closeup_perspective`): edit renders add them, framed on what changed; `render_views(focus_object_ids=...)` renders any views this way.
- **Normal and depth passes**: ray cast at up to 256 px and upscaled. Normals are world-space RGB with inverted faces in magenta; depth runs from bright (near) to dark (far), with the range in the artifact metadata.
- **Overlays** (`get_result(overlays=true)`): world grid with axis values and a scale bar in Blender units and mm (orthographic views), axis gizmo, view label. Drawn on the server; stored renders are unchanged.
- **Compare** (`get_result(compare=true)`): before | after panels against the parent revision's latest standard render. A dry run compares against its base revision.

### Protection

Revision 1 fingerprints every object of the upload: topology, coordinates, attributes, shape keys, vertex groups, transforms (world matrix, parenting, constraints), modifier settings, and evaluated geometry. Every edit is re-opened in a fresh Blender process and compared. Any difference, a missing protected object, or a component that cannot be evaluated reliably rejects the edit with object ids, reasons and per-component fix hints, and the current revision does not move. Objects the agent adds are free to change or remove later. Selection and hide state are ignored.

Each revision also stores the fingerprints of all its objects, so an edit reports which agent objects it `added`, `modified` and `removed` relative to its base. Revisions created before this was added only report added and removed objects.

### Coordinates

- World: Blender Z-up. The default viewing frame looks at the front from −Y (`front = [0, -1, 0]`, `up = [0, 0, 1]`). Changing the frame moves cameras, never the sculpture.
- Image probes: normalised `u`, `v` with the origin at the top-left corner, `u` right, `v` down.
- Every render artifact carries `matrix_world`, `projection`, clipping, image size, revision id, and frame.
- Face references are `<revision_id>:<object_id>:<face_index>` on the evaluated mesh, valid only for that revision.

### STL units

STL has no unit field. Coordinates are Blender units × `scene.unit_settings.scale_length` (metres), × 1000 for `mm` (default). `scene` writes raw Blender units. Visible mesh objects are exported with evaluated modifiers and world transforms applied to the export copy only.

## Storage and recovery

Uploads are stored byte-for-byte as `original.blend` and never modified. Dry-run renders are stored like other renders but belong to no revision. Reference images are re-encoded as PNG under `projects/<id>/references/`. Revisions, renders, and exports are staged per job, validated, renamed into place, and only then committed to SQLite. On restart, interrupted jobs are marked failed, staging is cleared, and files without a committed row are removed.

## Development

```sh
uv run ruff check src && uv run ruff format --check src
uv run mypy
uv run pytest            # tests marked `blender` need a real Blender 5.2
```

## Third-party assets

Tabler Icons (MIT), Barlow and Saira Stencil One (SIL OFL 1.1). Licenses are in `src/sculpt_workspace/web/static/`.
