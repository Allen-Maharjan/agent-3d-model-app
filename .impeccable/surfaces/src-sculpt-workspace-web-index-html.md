---
version: 1
slug: "src-sculpt-workspace-web-index-html"
primary_target: "src/sculpt_workspace/web/index.html"
related_targets: []
---

# Surface brief: Sculpt Workspace browser UI (Projects + Workspace)

Scope: single-page vanilla app with two screens, Projects and Workspace. Mode: Operate.
Audience: developer / technical artist supervising AI agents editing sculptures. Job: upload a self-contained .blend, copy the project UUID to an agent, watch jobs, inspect four renders, read revision + protection reports, check out revisions, download .blend / export STL.
Constraints: vanilla HTML/CSS/JS, no build step, no external CDNs (local app, offline), loopback only. Code-led build (no image generation).

## Direction contract

THESIS: Revisions are numbered blocks cut from one protected original, the way a Carrara quarry stencils lot numbers on every block. Refuses the category default of a dark dev dashboard with a blue accent and a job table.

OWN-WORLD: Marble-dust ground (cool pale grey, faint vein texture only in the empty drop zone), slate ink, iron-oxide red as the quarry mark reserved for protection status and the primary action, a quarry-blue second mark for running/selected state. One workhorse sans for UI; a stencil numeral treatment only for revision numbers (R1, R2...). Square-cornered plates, 1px slate hairlines, registration crosshairs on render plate corners. Job state in line form plus hue: dashed = queued, solid = running, struck = failed.

STORY: The visitor sees which revision is current, that the original is intact, what the agent last did, and whether anything is running; they can grab the UUID, step back to an older block, or take the model out.

FIRST VIEWPORT: Top bar (wordmark, Projects link, Blender status). Workspace: header row with project name, copyable UUID, current revision block number. Left 2/3: 2x2 registered render plates (front, right, back, perspective) at one fixed scale with view labels and camera notes. Right 1/3: job status strip, then revision rail of stencilled blocks with summary, protection verdict, checkout. Downloads (.blend, STL) in the header row as primary/secondary buttons.

FORM: Quarry block marking, candidate 7 of 7 on the ordered list; seed key 61a2626c. Raises: fixed-scale plate registration (Botanical Folio), plate-corner crosshairs (Design Annual), line-form job state (Emission Line Rail).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
