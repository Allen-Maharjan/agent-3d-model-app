# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI + Uvicorn backend, vanilla HTML/CSS/JavaScript frontend served by the same process, SQLite, local files, isolated Blender subprocesses. Started with `python -m sculpt_workspace` on `http://localhost:8765`; MCP at `/mcp`. No build step, no frontend framework (specified in the MVP plan).

## Users

Primary: a developer or technical artist building and debugging agent-driven sculpture workflows. They upload a self-contained `.blend`, hand its project UUID to an AI agent (Codex or Claude Code), and use the browser to watch jobs, inspect renders, read revision/protection reports, check out revisions, and export results. They are comfortable with UUIDs, job states, and logs, and need technical detail visible rather than hidden.

## Product Purpose

Let AI agents iterate on a sculpture (inspect → edit → render → review) while guaranteeing that geometry from the original upload is never altered. Success: an agent can add and refine ornaments across revisions, every revision is preserved and downloadable, and the human can verify what happened at a glance.

## Positioning

Protected-geometry editing: every agent edit is validated in a fresh Blender process against the original upload baseline, and rejected with object IDs and reasons if it touches protected geometry. Agents get spatial queries and camera-annotated renders to target meshes precisely.

## Operating Context

Runs locally on one machine (macOS or Linux), single user, loopback only. The browser has two screens: Projects (upload + recent projects) and Workspace (UUID, current revision, four renders, job status, revision history with checkout, `.blend` download, STL export). Agents connect over authenticated MCP with six tools: `inspect_project`, `apply_edit`, `render_views`, `get_result`, `checkout_revision`, `export_model`.

## Capabilities and Constraints

- 512 MiB upload limit; self-contained `.blend` only (packed assets, local data).
- Four standard views (front, right, back, perspective) at 1024 px, Workbench by default.
- One Blender subprocess at a time; durable jobs in SQLite.
- Out of scope: cloud hosting, anatomical recognition, geometry repair, printability optimisation, app-controlled AI iteration.
- Blender is not installed in the build environment; real integration tests are deferred to the user.

## Brand Commitments

Name: "Sculpt Workspace". No existing logo, palette, or voice; visual direction delegated.

## Evidence on Hand

None. No sample sculptures, screenshots, or user quotes. Do not fabricate.

## Product Principles

1. The original is sacred: protection status is always visible and never ambiguous.
2. History is never lost: every revision stays reachable and downloadable.
3. Show the machine's state honestly: job states, failures, and reasons are surfaced verbatim.
4. The agent does the work; the UI is a supervisor's console, not an editor.
