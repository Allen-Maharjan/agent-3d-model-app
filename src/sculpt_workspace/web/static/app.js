"use strict";

/* Sculpt Workspace browser client. Two screens: Projects and Workspace.
   All data goes through the same /api the server exposes; nothing here ever
   sees the MCP token. */

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const ACTIVE = new Set(["blocked", "queued", "running"]);
const STANDARD_VIEWS = ["front", "right", "back", "perspective"];
const POLL_ACTIVE_MS = 1500;
const POLL_IDLE_MS = 15000;

const main = document.getElementById("main");
const toastEl = document.getElementById("toast");
let screen = null; // current screen controller, has .destroy()
let maxUploadBytes = 512 * 1024 * 1024;

/* ---------- small helpers ---------- */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style") Object.assign(el.style, v); // CSSOM, allowed under the page CSP
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function icon(name, cls = "icon") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `/static/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}

function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("is-on");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => toastEl.classList.remove("is-on"), 3200);
}

function bytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function timeEl(iso) {
  return h("time", { datetime: iso, title: iso ? new Date(iso).toLocaleString() : "" }, ago(iso));
}

function duration(job) {
  if (!job.started_at) return "";
  const end = job.finished_at ? Date.parse(job.finished_at) : Date.now();
  const s = (end - Date.parse(job.started_at)) / 1000;
  return s < 60 ? `${s.toFixed(s < 10 ? 1 : 0)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

function block(number, opts = {}) {
  const cls = ["block", opts.size ? `block-${opts.size}` : "", opts.current ? "is-current" : "", opts.original ? "is-original" : ""];
  return h("span", { class: cls.filter(Boolean).join(" "), "aria-label": `Revision ${number}` }, `R${number}`);
}

function stateEl(state) {
  const label = { blocked: "Waiting", queued: "Queued", running: "Running", succeeded: "Done", failed: "Failed", cancelled: "Skipped" }[state] || state;
  return h("span", { class: "state", "data-state": state }, label);
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label} copied`);
  } catch {
    toast("Copy failed. Select the text and copy it manually.");
  }
}

/* ---------- API ---------- */

class ApiError extends Error {
  constructor(status, error) {
    super(error.message || `Request failed (${status})`);
    this.status = status;
    this.code = error.code;
    this.details = error.details || {};
  }
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(method !== "GET" ? { "X-CSRF-Token": CSRF } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.status === 401 && data?.error?.code === "session_required") {
    location.reload();
    throw new ApiError(401, data.error);
  }
  if (!res.ok) throw new ApiError(res.status, data?.error || { message: res.statusText });
  return data;
}

/* ---------- error explanations (problem + recovery) ---------- */

const RECOVERY = {
  invalid_file_type: "Choose a file that ends in .blend.",
  invalid_blend_file: "Open it in Blender, save it again with File > Save As, and upload the new file.",
  upload_too_large: "Purge unused data in Blender (File > Clean Up > Purge Unused Data) or remove reference meshes, then upload again.",
  not_self_contained: "In Blender: File > External Data > Pack Resources, make linked data local, save, and upload again.",
  unsupported_geometry: "Convert these objects to meshes (Object > Convert > Mesh) or delete them, then upload again.",
  empty_scene: "Add the sculpture to the scene and save before uploading.",
  blender_unavailable: "Install Blender 5.2 LTS or set SCULPT_BLENDER_PATH, then restart Sculpt Workspace.",
  blender_timeout: "The job hit its deadline. Raise the matching SCULPT_DEADLINE_* setting or simplify the scene.",
  blender_crashed: "Blender stopped unexpectedly. Check the job log in the data directory and retry.",
  interrupted: "The server restarted while this job ran. Submit it again.",
  queue_full: "Wait for running jobs to finish, then try again.",
  protected_geometry_modified: "Edits may change only objects they added. The protected objects this one touched are listed below, with fixes.",
  baseline_not_reproducible: "Blender evaluated the file differently on reload. Apply or remove non-deterministic modifiers and upload again.",
  stale_revision: "Someone moved the current revision. The page has been refreshed; try again.",
  mutation_pending: "Wait for the running edit to finish first.",
  no_visible_geometry: "Make at least one mesh visible for rendering, or check out a revision that has one.",
  nothing_to_export: "Make at least one mesh visible, then export again.",
  geometry_check_failed: "Fix the listed problems on the edit's own objects and try again.",
  edit_script_error: "The script failed before anything was saved. The error and line are below.",
  invalid_image: "Upload a PNG, JPEG, WebP, GIF or BMP image.",
  too_many_references: "Remove a reference image before adding another.",
};

function problem(err, title) {
  const code = err.code || "error";
  const box = h("div", { class: "problem", role: "alert" },
    h("div", { class: "problem-title" }, icon("alert-triangle"), title || err.message),
  );
  if (title && err.message) box.append(h("p", {}, err.message));
  if (RECOVERY[code]) box.append(h("p", {}, RECOVERY[code]));
  const d = err.details || {};
  const items = [];
  for (const i of (d.issues || []).slice(0, 8)) items.push(`${i.datablock} "${i.name}": ${i.message}`);
  for (const o of (d.objects || []).slice(0, 8)) items.push(`${o.name} (${o.id || o.object_id}): ${(o.reasons || o.issues || []).map((r) => r.message || r.reason || r).join("; ")}`);
  for (const v of (d.violations || []).slice(0, 8)) items.push(`${v.name || v.object_id} (${v.object_id}): ${v.reasons.join("; ")}${v.hints?.length ? `. Fix: ${v.hints[0]}` : ""}`);
  if (d.line) items.push(`Line ${d.line}${d.hint ? `. ${d.hint}` : ""}`);
  if (items.length) box.append(h("ul", {}, items.map((t) => h("li", {}, t))));
  const more = (d.issues || d.objects || d.violations || []).length - items.length;
  if (more > 0) box.append(h("p", {}, `${more} more not shown.`));
  return box;
}

async function uploadRaw(path, file) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": "application/octet-stream", "X-CSRF-Token": CSRF },
    body: file,
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw new ApiError(res.status, data?.error || { message: res.statusText });
  return data;
}

/* ---------- engine status ---------- */

async function refreshEngine() {
  const el = document.getElementById("engine-status");
  try {
    const health = await api("/api/health");
    maxUploadBytes = health.limits.max_upload_bytes;
    const b = health.blender;
    const q = health.queue;
    const notice = document.getElementById("engine-notice");
    notice.hidden = b.available;
    if (!b.available) {
      notice.replaceChildren(icon("alert-triangle"), h("p", {}, h("b", {}, "Blender is unavailable. "), b.message, " Restart Sculpt Workspace after fixing it."));
    }
    el.replaceChildren(
      q.outstanding ? h("span", { class: "engine-detail" }, `${q.outstanding} job${q.outstanding === 1 ? "" : "s"} in queue`) : "",
      h("span", { class: "engine-state", "data-ok": String(b.available), title: b.message },
        icon(b.available ? "cube" : "alert-triangle"),
        h("span", { class: "engine-label" }, b.available ? `Blender ${b.version}` : "Blender unavailable")),
    );
  } catch {
    el.replaceChildren(h("span", { class: "engine-state", "data-ok": "false" }, "Server unreachable"));
  }
}

/* ---------- Projects screen ---------- */

function projectsScreen() {
  const frag = document.getElementById("tpl-projects").content.cloneNode(true);
  main.replaceChildren(frag);
  document.title = "Projects · Sculpt Workspace";
  const drop = main.querySelector("[data-dropzone]");
  const input = main.querySelector("[data-file-input]");
  const intake = main.querySelector("[data-intake]");
  const recent = main.querySelector("[data-recent]");
  main.querySelector("[data-limit]").textContent = bytes(maxUploadBytes);
  let timer = null;
  let alive = true;
  let upload = null; // {file, progress, xhr, result, error, importJob, renderJob}

  function stop() {
    clearTimeout(timer);
  }

  async function loadRecent() {
    try {
      const { projects } = await api("/api/projects");
      renderRecent(projects);
      return projects.some((p) => p.status === "importing");
    } catch (err) {
      recent.replaceChildren(problem(err, "Could not load projects"));
      return false;
    }
  }

  function renderRecent(projects) {
    if (!projects.length) {
      recent.replaceChildren(h("div", { class: "empty" },
        h("strong", {}, "No sculptures yet"),
        h("p", {}, "Upload a .blend to create a project. You get a project UUID to hand to Codex or Claude Code, and the original geometry stays protected in every revision."),
      ));
      return;
    }
    const rows = projects.map((p) => {
      const href = `/projects/${p.project_id}`;
      const status = p.status === "ready"
        ? (p.current_revision_number ? block(p.current_revision_number, { size: "sm" }) : "")
        : p.status === "importing" ? stateEl("running") : stateEl("failed");
      return h("tr", { onclick: (e) => { if (!e.target.closest("a,button")) navigate(href); } },
        h("td", {},
          h("a", { class: "project-name", href, "data-nav": true }, p.name),
          h("div", { class: "cell-sub mono" }, p.project_id),
        ),
        h("td", {}, status, p.status === "import_failed" ? h("div", { class: "cell-sub" }, p.error?.message || "Import failed", RECOVERY[p.error?.code] ? ` ${RECOVERY[p.error.code]}` : "") : ""),
        h("td", { class: "num col-optional" }, `${p.revision_count}`),
        h("td", { class: "num col-optional" }, bytes(p.original_size)),
        h("td", { class: "num" }, timeEl(p.updated_at)),
      );
    });
    recent.replaceChildren(h("table", { class: "table" },
      h("thead", {}, h("tr", {},
        h("th", { scope: "col" }, "Project"),
        h("th", { scope: "col" }, "Revision"),
        h("th", { scope: "col", class: "num col-optional" }, "Revisions"),
        h("th", { scope: "col", class: "num col-optional" }, "Upload size"),
        h("th", { scope: "col", class: "num" }, "Updated"),
      )),
      h("tbody", {}, rows),
    ));
  }

  function stage(label, state, detail) {
    return h("li", { class: "stage", "data-state": state }, stateEl(state), h("span", {}, label), h("span", { class: "stage-detail" }, detail || ""));
  }

  function renderIntake() {
    if (!upload) {
      intake.hidden = true;
      return;
    }
    intake.hidden = false;
    const u = upload;
    const upState = u.error && !u.result ? "failed" : u.result ? "succeeded" : "running";
    const imp = u.importJob;
    const ren = u.renderJob;
    const list = h("ol", { class: "stages", "aria-label": "Upload progress" },
      stage("Upload", upState, u.result ? bytes(u.file.size) : `${Math.round(u.progress * 100)}%`),
    );
    if (upState === "running") {
      list.append(h("li", {}, h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(u.progress * 100)), "aria-label": "Upload" },
        h("span", { style: { transform: `scaleX(${u.progress})` } }))));
    }
    list.append(
      stage("Import and fingerprint original", imp ? imp.state : "queued", imp?.state === "running" ? duration(imp) : ""),
      stage("Render four views", ren ? ren.state : "queued", ren?.state === "running" ? duration(ren) : ""),
    );
    const children = [h("div", { class: "intake-file" }, h("span", {}, u.file.name), h("small", {}, bytes(u.file.size)))];
    if (u.result) {
      children.push(h("dl", { class: "intake-ids" },
        h("dt", {}, "Project UUID"),
        h("dd", {}, h("code", {}, u.result.project_id),
          h("button", { class: "btn btn-ghost", type: "button", "aria-label": "Copy project UUID", title: "Copy project UUID", onclick: () => copyText(u.result.project_id, "Project UUID") }, icon("copy"))),
        h("dt", {}, "Import job"),
        h("dd", {}, h("code", {}, u.result.import_job_id)),
      ));
    }
    children.push(list);
    const err = u.error || imp?.error || (ren?.state === "failed" ? ren.error : null);
    if (err) children.push(problem(err, imp?.state === "failed" ? "The file could not be imported" : u.result ? "Rendering failed" : "Upload failed"));
    const actions = h("div", { class: "intake-actions" });
    if (u.result) {
      actions.append(
        h("a", { class: "btn btn-primary", href: `/projects/${u.result.project_id}`, "data-nav": true }, "Open workspace"),
      );
    }
    if (err || ren?.state === "succeeded") {
      actions.append(h("button", { class: "btn btn-ghost", type: "button", onclick: () => { upload = null; renderIntake(); input.focus(); } }, "Upload another"));
    }
    if (actions.childElementCount) children.push(actions);
    intake.replaceChildren(...children);
  }

  async function pollJobs() {
    if (!upload?.result) return false;
    const [imp, ren] = await Promise.all([
      api(`/api/jobs/${upload.result.import_job_id}`),
      api(`/api/jobs/${upload.result.render_job_id}`),
    ]);
    upload.importJob = imp;
    upload.renderJob = ren;
    renderIntake();
    return ACTIVE.has(imp.state) || ACTIVE.has(ren.state);
  }

  async function tick() {
    if (!alive) return;
    let busy = false;
    try {
      const [a, b] = await Promise.all([loadRecent(), pollJobs()]);
      busy = a || b || (upload && !upload.result && !upload.error);
    } catch {
      busy = true;
    }
    if (alive) timer = setTimeout(tick, busy ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  function start(file) {
    if (upload && !upload.result && !upload.error) return;
    if (!file.name.toLowerCase().endsWith(".blend")) {
      upload = { file, progress: 0, error: { code: "invalid_file_type", message: `${file.name} is not a .blend file.` } };
      renderIntake();
      return;
    }
    if (file.size > maxUploadBytes) {
      upload = { file, progress: 0, error: { code: "upload_too_large", message: `${bytes(file.size)} is over the ${bytes(maxUploadBytes)} limit.` } };
      renderIntake();
      return;
    }
    upload = { file, progress: 0 };
    renderIntake();
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("X-CSRF-Token", CSRF);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && upload) {
        upload.progress = e.loaded / e.total;
        renderIntake();
      }
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { data = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        upload.result = data;
        upload.progress = 1;
        toast("Uploaded. Importing in Blender.");
      } else {
        upload.error = data?.error || { message: `Upload failed (${xhr.status})` };
      }
      renderIntake();
      stop();
      tick();
    };
    xhr.onerror = () => {
      upload.error = { message: "The connection to Sculpt Workspace was lost during upload." };
      renderIntake();
    };
    xhr.send(file);
  }

  input.addEventListener("change", () => {
    if (input.files[0]) start(input.files[0]);
    input.value = "";
  });
  for (const ev of ["dragenter", "dragover"]) {
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    drop.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "dragleave" && drop.contains(e.relatedTarget)) return; drop.classList.remove("is-over"); });
  }
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) start(file);
  });

  recent.replaceChildren(h("div", { class: "empty", "aria-busy": "true" }, h("div", { class: "skeleton-line" }), h("div", { class: "skeleton-line", style: { width: "60%" } })));
  tick();
  return { destroy() { alive = false; stop(); } };
}

/* ---------- Workspace screen ---------- */

const JOB_LABEL = { import: "Import", edit: "Agent edit", render: "Render", inspect: "Spatial query", export: "STL export" };
const REJECTED = new Set(["protected_geometry_modified", "geometry_check_failed"]);

function jobLabel(j) {
  if (j.kind !== "edit") return JOB_LABEL[j.kind] || j.kind;
  const mine = j.params?.author === "user";
  if (j.params?.dry_run) return mine ? "Your dry run" : "Dry run";
  return mine ? "Your edit" : JOB_LABEL.edit;
}

const AUTHOR_LABEL = { agent: "Agent", user: "You", upload: "Original upload" };

const STARTER_SCRIPT = `# Edit script: runs inside Blender against the current revision.
# The original sculpture is protected and read-only; build new objects and attach them to it.
# Ctrl/Cmd+Enter starts a dry run. The helper reference is below the editor.

lo, hi = sculpt.scene_bounds()
size = hi - lo
print("sculpture size (mm):", sculpt.to_mm(size))
`;

const SCRIPT_REFERENCE = `Predefined: bpy, bmesh, mathutils, Vector, Matrix, math, sculpt. print() output is returned.

sculpt.objects() -> {object_id: Object}    sculpt.get(id)    sculpt.id_of(obj)
sculpt.protected_ids    sculpt.is_protected(obj_or_id)    sculpt.link(obj, collection=None)
sculpt.scene_bounds() -> (min, max) of the original sculpture
sculpt.mm(x), sculpt.cm(x) -> Blender units    sculpt.to_mm(units_or_vector)
sculpt.ray(origin, direction, object_ids=None, max_distance=None) -> hit or None
sculpt.nearest(point, object_ids=None, max_distance=None) -> hit or None
    hit: position, normal, distance, object_id, object_name, face_index
sculpt.place_on_surface(obj, position, normal, offset=0.0, axis="Z", up="Y", spin=0.0)
sculpt.duplicate(obj, linked=False, name=None)    sculpt.array_along(obj, positions, normals=None)
sculpt.from_curve(points, radius=0.01, name="Curve", cyclic=False, smooth=True)
sculpt.conform_to(obj, target_id, offset=0.0, method="NEAREST_SURFACEPOINT")
sculpt.boolean(obj, cutter, operation="UNION")
sculpt.material(name, color, metallic=0.0, roughness=0.5, emission=None)
sculpt.assign_material(obj, material)    sculpt.apply_modifiers(obj) -> object to use

Helpers refuse to change protected objects. Avoid bpy.ops: background Blender has no UI context.`;

const REDUCE_MOTION = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

/* "+12 -3 lines" that counts up from zero, so a code change registers at a glance. */
function countUp(el, added, removed, tail, animate = true) {
  cancelAnimationFrame(el._raf);
  const paint = (k) => {
    el.replaceChildren(
      h("span", { class: "stat-add" }, `+${Math.round(added * k)}`),
      " ",
      h("span", { class: "stat-del" }, `−${Math.round(removed * k)}`),
      ` ${tail}`,
    );
  };
  if (!animate || REDUCE_MOTION || (!added && !removed)) return paint(1);
  const start = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - start) / 600);
    paint(1 - (1 - t) ** 3);
    if (t < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

function changeLine(changes) {
  if (!changes) return "";
  const parts = [];
  if (changes.added?.length) parts.push(`${changes.added.length} added`);
  if (changes.modified?.length) parts.push(`${changes.modified.length} changed`);
  if (changes.removed?.length) parts.push(`${changes.removed.length} removed`);
  return parts.length ? `Objects: ${parts.join(", ")}` : "No agent objects changed";
}

function checkItems(checks) {
  return (checks || []).flatMap((c) => c.issues.map((i) => h("li", { "data-severity": i.severity }, `${c.name}: ${i.message}`)));
}

function axisName(v) {
  const names = [["+X", [1, 0, 0]], ["−X", [-1, 0, 0]], ["+Y", [0, 1, 0]], ["−Y", [0, -1, 0]], ["+Z", [0, 0, 1]], ["−Z", [0, 0, -1]]];
  for (const [n, a] of names) if (a.every((c, i) => Math.abs(c - v[i]) < 1e-3)) return n;
  return `(${v.map((c) => c.toFixed(2)).join(", ")})`;
}

function workspaceScreen(projectId) {
  let alive = true;
  let timer = null;
  let data = null;
  const sig = {};
  let shading = "color"; // or "highlight": which pass the plates show
  let jobsExpanded = false;
  let history = []; // pages from /jobs, loaded only when the full list is open
  const root = h("div", { class: "workspace" });
  const head = h("div");
  const renders = h("section", { class: "renders", "aria-labelledby": "renders-title" });
  const brief = h("section", { class: "rail-section", "aria-labelledby": "brief-title" });
  const jobs = h("section", { class: "rail-section", "aria-labelledby": "jobs-title" });
  const revs = h("section", { class: "rail-section", "aria-labelledby": "revs-title" });
  const codeSlot = h("div", { class: "code-slot" });
  const live = h("div", { class: "visually-hidden", role: "status", "aria-live": "polite", "aria-atomic": "true" });
  root.append(
    h("a", { class: "back", href: "/", "data-nav": true }, icon("arrow-left"), "Projects"),
    head,
    h("div", { class: "ws-body" }, h("div", { class: "ws-main" }, renders, codeSlot), h("aside", { class: "rail", "aria-label": "Brief, jobs and revisions" }, brief, jobs, revs)),
    live,
  );
  main.replaceChildren(root);
  head.append(h("div", { class: "ws-head" }, h("div", { class: "skeleton-line", style: { width: "40%", height: "28px" } })));

  // Re-render a panel when its data changes. The signature is recorded only
  // after a successful render, and a failing panel shows its own error so the
  // other panels still render and the next poll retries it.
  function patch(key, el, value, render) {
    const s = JSON.stringify(value);
    if (sig[key] === s) return;
    try {
      el.replaceChildren(...[].concat(render()));
      sig[key] = s;
    } catch (err) {
      sig[key] = null;
      console.error(`Could not render the ${key} panel`, err);
      el.replaceChildren(problem({ code: "render_error", message: `This panel could not be displayed: ${err.message}` }));
    }
  }

  async function act(button, fn) {
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      await fn();
    } catch (err) {
      if (err.code === "stale_revision") await load();
      toast(err.message + (RECOVERY[err.code] ? ` ${RECOVERY[err.code]}` : ""));
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  function renderHead() {
    const p = data.project;
    const cur = data.current_revision;
    const stl = h("select", { class: "select", "aria-label": "STL units" },
      h("option", { value: "mm" }, "STL in mm"), h("option", { value: "m" }, "STL in m"), h("option", { value: "scene" }, "STL in scene units"));
    const exportBtn = h("button", { class: "btn btn-secondary", type: "button", disabled: !cur }, icon("file-export"), "Export STL");
    exportBtn.addEventListener("click", () => act(exportBtn, async () => {
      await api(`/api/projects/${projectId}/exports`, { method: "POST", body: { revision_id: cur.revision_id, format: "stl", stl_units: stl.value } });
      toast(`Exporting R${cur.number} as STL`);
      await load();
    }));
    return h("div", { class: "ws-head" },
      cur ? block(cur.number, { size: "lg", current: true, original: cur.number === 1 }) : h("span", { class: "block block-lg block-empty", "aria-label": "No revision yet" }, "R–"),
      h("div", { class: "ws-title" },
        h("h1", { class: "page-title" }, p.name),
        h("div", { class: "uuid" },
          h("code", {}, p.project_id),
          h("button", { class: "btn btn-ghost", type: "button", "aria-label": "Copy project UUID", title: "Copy project UUID", onclick: () => copyText(p.project_id, "Project UUID") }, icon("copy")),
        ),
      ),
      h("div", { class: "ws-actions" },
        h("span", { class: "export-group" }, stl, exportBtn),
        cur
          ? h("a", { class: "btn btn-primary", href: `/api/projects/${projectId}/revisions/${cur.revision_id}/model.blend` }, icon("download"), `Download R${cur.number} .blend`)
          : h("span", { class: "btn btn-primary", "aria-disabled": "true" }, icon("download"), "Download .blend"),
      ),
    );
  }

  function renderPlates() {
    const p = data.project;
    const cur = data.current_revision;
    const r = data.renders;
    const out = [];
    const headRow = h("div", { class: "renders-head" }, h("h2", { id: "renders-title", class: "section-title" }, "Views"));
    out.push(headRow);
    if (!cur) {
      const failed = p.status === "import_failed";
      out.push(h("div", { class: "plates" }, STANDARD_VIEWS.map((v) => h("figure", { class: "plate" },
        h("div", { class: `plate-frame ${failed ? "is-pending" : "is-loading"}` }, failed ? h("span", { class: "plate-note" }, "Not rendered") : ""),
        h("figcaption", { class: "plate-cap" }, h("b", {}, v))))));
      if (failed) out.unshift(problem(p.error || {}, "The file could not be imported"));
      return out;
    }
    const latest = r?.latest_job;
    const images = r?.images || [];
    const showing = images[0]?.revision_id;
    const meta = images[0]?.metadata;
    if (meta) {
      const f = meta.frame;
      headRow.append(h("p", { class: "renders-meta" }, `R${cur.number}, ${meta.engine === "BLENDER_WORKBENCH" ? "Workbench" : "Eevee"}, ${meta.image.width} px, front from ${axisName(f.front)} and up ${axisName(f.up)}`));
    }
    if (latest && ACTIVE.has(latest.state) && images.length) {
      out.push(h("p", { class: "stale-note" }, stateEl("running"), "Rendering new views. Showing the previous render."));
    }
    if (latest?.state === "failed" || latest?.state === "cancelled") {
      const retry = h("button", { class: "btn btn-secondary", type: "button" }, icon("refresh"), "Retry render");
      retry.addEventListener("click", () => act(retry, async () => {
        await api(`/api/projects/${projectId}/renders`, { method: "POST", body: { revision_id: cur.revision_id } });
        toast(`Rendering R${cur.number}`);
        await load();
      }));
      const box = problem(latest.error || {}, "Rendering failed. The revision is saved and downloadable.");
      box.append(h("div", { class: "intake-actions" }, retry));
      out.push(box);
    }
    const byView = Object.fromEntries(images.map((a) => [a.view, a]));
    const pending = latest && ACTIVE.has(latest.state);
    const hasHighlight = images.some((a) => a.view.endsWith("--highlight"));
    if (hasHighlight) {
      const seg = h("div", { class: "seg", role: "group", "aria-label": "Plate shading" });
      for (const [mode, label] of [["color", "Shaded"], ["highlight", "Changes"]]) {
        const b = h("button", { class: "btn btn-ghost", type: "button", "aria-pressed": String(shading === mode) }, label);
        b.addEventListener("click", () => { shading = mode; sig.renders = null; load(); });
        seg.append(b);
      }
      headRow.append(seg);
    }
    const pick = (v) => (shading === "highlight" && byView[`${v}--highlight`]) || byView[v];
    if (shading === "highlight" && hasHighlight) {
      out.push(h("p", { class: "legend" },
        h("span", { class: "swatch swatch-new" }), "New or changed in this revision",
        h("span", { class: "swatch swatch-agent" }), "Earlier agent objects",
        h("span", { class: "swatch swatch-original" }), "Original"));
    }
    const plate = (v, a, caption) => {
      const cam = a.metadata.camera;
      const lens = cam.type === "orthographic" ? "Orthographic" : `Perspective ${Math.round(cam.lens_mm)} mm`;
      return h("figure", { class: "plate" },
        h("div", { class: "plate-frame" },
          h("a", { href: a.url, target: "_blank", rel: "noopener", "aria-label": `Open ${caption} at full size` },
            h("img", { src: a.url, alt: `${caption} of revision R${cur.number}`, width: a.metadata.image.width, height: a.metadata.image.height, loading: "lazy", decoding: "async" }))),
        h("figcaption", { class: "plate-cap" }, h("b", {}, caption), h("span", {}, lens)));
    };
    out.push(h("div", { class: "plates" }, STANDARD_VIEWS.map((v) => {
      const a = pick(v);
      if (a) return plate(v, a, v);
      return h("figure", { class: "plate" },
        h("div", { class: `plate-frame ${pending ? "is-loading" : "is-pending"}` }, pending ? "" : h("span", { class: "plate-note" }, "No render yet")),
        h("figcaption", { class: "plate-cap" }, h("b", {}, v), h("span", {}, pending ? "Rendering" : "")));
    })));
    const closeups = images.filter((a) => a.view.startsWith("closeup_") && !a.view.includes("--")).map((a) => a.view);
    if (closeups.length) {
      out.push(h("h3", { class: "subsection-title" }, "Close-ups of what changed"));
      out.push(h("div", { class: "plates" }, closeups.map((v) => plate(v, pick(v), v.replace("closeup_", "")))));
    }
    if (showing && showing !== cur.revision_id) out.push(h("p", { class: "renders-meta" }, "Images belong to a different revision."));
    return out;
  }

  function jobSub(j) {
    const rev = data.revisions.find((r) => r.revision_id === (j.revision_id || j.base_revision_id));
    const parts = [];
    if (rev) parts.push(`R${rev.number}`);
    if (j.state === "running") parts.push(`running ${duration(j)}`);
    else if (j.finished_at && j.started_at) parts.push(duration(j));
    return h("div", { class: "job-sub" }, parts.join(", "), parts.length ? ", " : "", timeEl(j.created_at));
  }

  async function loadHistory(offset) {
    const page = await api(`/api/projects/${projectId}/jobs?offset=${offset}&limit=50`);
    history = offset === 0 ? page.jobs : history.concat(page.jobs);
    return page.total;
  }

  function renderJobs() {
    // Active jobs always come first, however old, so a long-running job is never hidden.
    const activeJobs = data.jobs.filter((j) => ACTIVE.has(j.state));
    const done = data.jobs.filter((j) => !ACTIVE.has(j.state));
    const active = activeJobs.length;
    let list;
    if (jobsExpanded) {
      // Prefer the polled copy of a job; a history entry still marked active but
      // missing from the poll (which includes every active job) has since finished.
      const fresh = Object.fromEntries(data.jobs.map((j) => [j.job_id, j]));
      const seen = new Set();
      list = [];
      for (const j of activeJobs.concat(done, history.map((x) => fresh[x.job_id] || x))) {
        if (seen.has(j.job_id) || (ACTIVE.has(j.state) && !fresh[j.job_id])) continue;
        seen.add(j.job_id);
        list.push(j);
      }
    } else {
      list = activeJobs.concat(done.slice(0, 8));
    }
    const total = data.jobs_total ?? data.jobs.length;
    const items = list.map((j) => {
      const li = h("li", { class: "job" },
        h("div", { class: "job-line" }, h("span", { class: "job-kind" }, jobLabel(j)), stateEl(j.state)),
        jobSub(j));
      if (j.kind === "edit" && j.params?.summary) li.append(h("div", { class: "job-sub" }, j.params.summary));
      if (j.state === "failed" && j.error) li.append(problem(j.error, j.kind === "edit" && REJECTED.has(j.error.code) ? "Edit rejected" : null));
      if (j.kind === "edit" && j.state === "succeeded" && j.result) {
        li.append(h("div", { class: "job-sub" }, changeLine(j.result.changes), j.params?.dry_run ? ". Not published (dry run)." : ""));
        const items = checkItems(j.result.checks);
        if (items.length) li.append(h("details", { class: "report" }, h("summary", {}, `Geometry checks (${items.length})`), h("ul", { class: "checks" }, items)));
      }
      if (j.kind === "export" && j.state === "succeeded" && j.result?.artifact_id) {
        li.append(h("div", { class: "intake-actions" },
          h("a", { class: "btn btn-secondary", href: `/api/artifacts/${j.result.artifact_id}?download=1` }, icon("download"), `Download STL (${j.result.triangles.toLocaleString()} triangles, ${j.result.units})`)));
      }
      if (j.kind === "inspect" && j.result) {
        // Blender returns {query_type, result}; show the whole payload if that shape is absent.
        const payload = j.result.result !== undefined ? j.result.result : j.result;
        const text = JSON.stringify(payload, null, 1) ?? "";
        li.append(h("details", { class: "report" }, h("summary", {}, "Query result"), h("pre", {}, text.slice(0, 4000))));
      }
      if (j.kind === "edit" && j.result?.script_output) {
        li.append(h("details", { class: "report" }, h("summary", {}, "Script output"), h("pre", {}, j.result.script_output)));
      }
      return li;
    });
    const more = [];
    if (!jobsExpanded && total > list.length) {
      const btn = h("button", { class: "btn btn-ghost", type: "button" }, icon("history"), `Show all ${total} jobs`);
      btn.addEventListener("click", () => act(btn, async () => {
        await loadHistory(0);
        jobsExpanded = true;
        sig.jobs = null;
        await load();
      }));
      more.push(btn);
    } else if (jobsExpanded) {
      if (history.length < total) {
        const older = h("button", { class: "btn btn-ghost", type: "button" }, icon("history"), "Load older jobs");
        older.addEventListener("click", () => act(older, async () => {
          await loadHistory(history.length);
          sig.jobs = null;
          await load();
        }));
        more.push(older);
      }
      const less = h("button", { class: "btn btn-ghost", type: "button" }, "Show recent only");
      less.addEventListener("click", () => {
        jobsExpanded = false;
        history = [];
        sig.jobs = null;
        load();
      });
      more.push(less);
    }
    return [
      h("h2", { id: "jobs-title", class: "section-title" }, "Jobs", h("small", {}, active ? `${active} active` : "")),
      items.length ? h("ul", { class: "jobs" }, items) : h("div", { class: "empty" }, h("p", {}, "No jobs yet. Agent edits, renders, and exports appear here.")),
      more.length ? h("div", { class: "intake-actions" }, more) : "",
    ];
  }

  function renderBrief() {
    const p = data.project;
    const text = h("textarea", {
      class: "textarea", id: "brief-text", rows: 4, maxlength: 4000,
      placeholder: "What should the agent add? Where on the sculpture, how big, which material, and which views must show it.",
    });
    text.value = p.brief || "";
    const save = h("button", { class: "btn btn-secondary", type: "button" }, icon("check"), "Save brief");
    save.addEventListener("click", () => act(save, async () => {
      await api(`/api/projects/${projectId}/brief`, { method: "PUT", body: { brief: text.value } });
      toast("Brief saved. Agents read it with inspect_project.");
      await load();
    }));
    const note = h("input", { class: "input", type: "text", maxlength: 500, placeholder: "Note for the next image (optional)", "aria-label": "Reference note" });
    const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/webp,image/gif,image/bmp", class: "visually-hidden" });
    const add = h("label", { class: "btn btn-secondary" }, file, icon("upload"), "Add reference");
    file.addEventListener("change", () => act(add, async () => {
      const f = file.files[0];
      if (!f) return;
      await uploadRaw(`/api/projects/${projectId}/references?filename=${encodeURIComponent(f.name)}&note=${encodeURIComponent(note.value)}`, f);
      toast("Reference added");
      await load();
    }));
    const refs = (p.references || []).map((r) => {
      const remove = h("button", { class: "btn btn-ghost", type: "button", "aria-label": `Remove reference ${r.filename}`, title: "Remove" }, icon("x"));
      remove.addEventListener("click", () => act(remove, async () => {
        await api(`/api/projects/${projectId}/references/${r.reference_id}`, { method: "DELETE" });
        await load();
      }));
      return h("figure", { class: "ref" },
        h("a", { href: r.url, target: "_blank", rel: "noopener" }, h("img", { src: r.url, alt: r.note || r.filename, width: r.width, height: r.height, loading: "lazy" })),
        h("figcaption", {}, h("span", {}, r.note || r.filename), remove));
    });
    return [
      h("h2", { id: "brief-title", class: "section-title" }, "Brief", h("small", {}, "read by agents")),
      h("label", { class: "visually-hidden", for: "brief-text" }, "Project brief"),
      text,
      h("div", { class: "intake-actions" }, save),
      refs.length ? h("div", { class: "refs" }, refs) : "",
      h("div", { class: "ref-add" }, note, add),
    ];
  }

  function renderRevisions() {
    const cur = data.current_revision;
    const byId = Object.fromEntries(data.revisions.map((r) => [r.revision_id, r]));
    const items = data.revisions.map((r) => {
      const isCur = cur && r.revision_id === cur.revision_id;
      const pr = r.protection;
      const guard = r.number === 1 || pr.baseline
        ? h("div", { class: "rev-guard" }, icon("lock"), `Original: ${pr.protected_objects ?? "?"} protected object${pr.protected_objects === 1 ? "" : "s"}`)
        : h("div", { class: "rev-guard" }, icon("shield-check"), `Original intact, ${pr.agent_objects.length} agent object${pr.agent_objects.length === 1 ? "" : "s"}`);
      const actions = h("div", { class: "rev-actions" });
      if (!isCur && cur) {
        const btn = h("button", { class: "btn btn-ghost", type: "button" }, icon("history"), "Check out");
        btn.addEventListener("click", () => act(btn, async () => {
          await api(`/api/projects/${projectId}/checkout`, { method: "POST", body: { revision_id: r.revision_id, expected_current_revision_id: cur.revision_id } });
          toast(`R${r.number} is now current. The next edit builds on it.`);
          await load();
        }));
        actions.append(btn);
      }
      if (r.has_source) {
        actions.append(h("button", { class: "btn btn-ghost", type: "button", onclick: () => code.showRevision(r.revision_id) }, icon("cube"), "View code"));
      }
      actions.append(h("a", { class: "btn btn-ghost", href: `/api/projects/${projectId}/revisions/${r.revision_id}/model.blend`, "aria-label": `Download R${r.number} .blend` }, icon("download"), ".blend"));
      const parent = r.parent_id ? byId[r.parent_id] : null;
      return h("li", { class: `rev${isCur ? " is-current" : ""}` },
        block(r.number, { size: "sm", current: isCur, original: r.number === 1 }),
        h("div", { class: "rev-body" },
          h("div", { class: "rev-summary" }, r.summary),
          h("div", { class: "rev-meta" }, h("span", { class: "author-chip", "data-author": r.author }, AUTHOR_LABEL[r.author] || r.author), timeEl(r.created_at), parent ? h("span", {}, `from R${parent.number}`) : "", h("span", { class: "mono" }, r.revision_id)),
          guard, r.protection.changes ? h("div", { class: "rev-meta" }, changeLine(r.protection.changes)) : "", actions));
    });
    return [
      h("h2", { id: "revs-title", class: "section-title" }, "Revisions", h("small", {}, data.revisions.length ? `${data.revisions.length} kept` : "")),
      items.length ? h("ol", { class: "revs", reversed: true }, items) : h("div", { class: "empty" }, h("p", {}, "Revision 1 appears once the import finishes.")),
    ];
  }

  /* ---------- code panel ----------
     Built once and updated in place (never through patch()), so re-renders
     never wipe what the user is typing. Shows the script behind a revision,
     animates what changed against its parent, and lets the user dry-run and
     publish their own edits through the same validation as agent edits. */
  function codePanel() {
    const draftKey = `sculpt-draft:${projectId}`;
    const sources = new Map(); // revision id -> script or null; revisions are immutable
    const st = {
      ready: false, viewRev: null, base: "", parentText: "", dirty: false, loading: 0,
      lastCurrent: null, dry: null, awaiting: null, revSig: "",
    };
    const section = h("section", { class: "code-panel", "aria-labelledby": "code-title" });
    const select = h("select", { class: "select", "aria-label": "Show the script of revision" });
    const author = h("span", { class: "author-chip" });
    const stats = h("span", { class: "code-stats" });
    const caption = h("p", { class: "code-caption" });
    const banner = h("div", { class: "code-banner", role: "status", hidden: true });
    const host = h("div", { class: "code-editor" });
    const summary = h("input", { class: "input", type: "text", maxlength: 500, placeholder: "Summary of this edit, shown in Revisions", "aria-label": "Edit summary" });
    const strictBox = h("input", { type: "checkbox", id: "code-strict" });
    const dryBtn = h("button", { class: "btn btn-secondary", type: "button" }, icon("refresh"), "Dry run");
    const pubBtn = h("button", { class: "btn btn-primary", type: "button" }, icon("check"), "Publish");
    const revertBtn = h("button", { class: "btn btn-ghost", type: "button", disabled: true }, icon("history"), "Revert");
    const result = h("div", { class: "code-result", "aria-live": "polite" });
    const live = h("div", { class: "visually-hidden", role: "status", "aria-live": "polite" });

    section.append(
      h("div", { class: "code-head" },
        h("h2", { id: "code-title", class: "section-title" }, "Code"),
        select, author, stats, revertBtn),
      caption, banner, host,
      h("div", { class: "code-controls" },
        summary,
        h("label", { class: "check", for: "code-strict" }, strictBox, "Strict geometry checks"),
        dryBtn, pubBtn),
      h("p", { class: "code-hint" }, "Dry run validates and renders without saving. Publish creates the next revision on top of the current one; the original stays protected either way."),
      result,
      h("details", { class: "report code-reference" }, h("summary", {}, "Script reference"), h("pre", {}, SCRIPT_REFERENCE)),
      live,
    );

    const editor = createCodeEditor(host, {
      value: "",
      label: "Edit script",
      onChange: () => scheduleDirty(),
      onRun: () => dryBtn.click(),
      onEscape: () => summary.focus(),
    });

    const revById = (id) => data?.revisions.find((r) => r.revision_id === id);
    const current = () => data?.current_revision;

    async function sourceOf(rev) {
      if (!rev || !rev.has_source) return null;
      if (!sources.has(rev.revision_id)) {
        const res = await api(`/api/projects/${projectId}/revisions/${rev.revision_id}/source`);
        sources.set(rev.revision_id, res.source ?? null);
      }
      return sources.get(rev.revision_id);
    }

    function saveDraft() {
      try {
        if (st.dirty) localStorage.setItem(draftKey, JSON.stringify({ text: editor.getValue(), rev: st.viewRev }));
        else localStorage.removeItem(draftKey);
      } catch { /* storage unavailable: drafts are a convenience only */ }
    }

    function readDraft() {
      try { return JSON.parse(localStorage.getItem(draftKey) || "null"); } catch { return null; }
    }

    let dirtyTimer = null;
    function scheduleDirty() {
      clearTimeout(dirtyTimer);
      dirtyTimer = setTimeout(updateDirty, 120);
    }

    function updateDirty() {
      const text = editor.getValue();
      const wasDirty = st.dirty;
      st.dirty = text !== st.base;
      editor.clearError();
      if (st.dirty) {
        const d = lineDiff(st.base, text);
        editor.markLines(d.added, "edited", false);
        countUp(stats, d.added.size, d.removed, "lines, not published", false);
        stats.dataset.kind = "edited";
      } else if (wasDirty) {
        markAgainstParent(false);
      }
      revertBtn.disabled = !st.dirty;
      saveDraft();
      syncButtons();
    }

    function markAgainstParent(animate) {
      const rev = revById(st.viewRev);
      stats.dataset.kind = "added";
      if (!rev?.has_source) {
        editor.markLines(new Set(), "added", false);
        stats.textContent = rev ? "Starter script" : "";
        return;
      }
      const d = lineDiff(st.parentText, st.base);
      editor.markLines(d.added, "added", animate);
      const parent = revById(rev.parent_id);
      countUp(stats, d.added.size, d.removed, parent ? `lines vs R${parent.number}` : "lines", animate);
    }

    function describe(rev) {
      const cur = current();
      author.textContent = rev ? AUTHOR_LABEL[rev.author] || rev.author : "";
      author.dataset.author = rev?.author || "";
      if (!rev) { caption.textContent = ""; return; }
      const onto = cur ? `Publishing applies it to the current revision, R${cur.number}.` : "";
      caption.textContent = rev.has_source
        ? `The script that made R${rev.number} from R${revById(rev.parent_id)?.number ?? "?"}. ${onto}`
        : `R${rev.number} is the original upload and has no script. Start from this template. ${onto}`;
    }

    /* Load a revision's script. Animated loads play the carve over the lines it added. */
    async function show(revId, { animate = true, text = null } = {}) {
      const ticket = ++st.loading;
      const rev = revById(revId);
      if (!rev) return;
      let src;
      let parentSrc;
      try {
        src = await sourceOf(rev);
        parentSrc = (await sourceOf(revById(rev.parent_id))) || "";
      } catch (err) {
        toast(`Could not load the script of R${rev.number}: ${err.message}`);
        return;
      }
      if (ticket !== st.loading) return; // a newer load superseded this one
      st.viewRev = revId;
      st.base = src ?? STARTER_SCRIPT;
      st.parentText = parentSrc;
      editor.setValue(text ?? st.base);
      editor.clearError();
      select.value = revId;
      describe(rev);
      hideBanner();
      if (text != null && text !== st.base) {
        updateDirty();
      } else {
        st.dirty = false;
        revertBtn.disabled = true;
        markAgainstParent(animate);
        saveDraft();
      }
      syncButtons();
      editor.refresh();
      if (animate && rev.has_source) live.textContent = `Showing the script of R${rev.number}`;
    }

    function hideBanner() { banner.hidden = true; banner.replaceChildren(); }

    function showBanner(rev) {
      const load = h("button", { class: "btn btn-secondary", type: "button" }, `Load R${rev.number} script`);
      load.addEventListener("click", () => show(rev.revision_id));
      banner.replaceChildren(
        icon("info-circle"),
        h("span", {}, `${AUTHOR_LABEL[rev.author] || "Someone"} published R${rev.number}. Your unpublished draft is kept; it will be applied on top of R${rev.number} if you publish it.`),
        load);
      banner.hidden = false;
    }

    function syncButtons() {
      const cur = current();
      const busy = st.dry && ACTIVE.has(st.dry.state);
      const ready = Boolean(cur) && data?.project.status === "ready";
      dryBtn.disabled = !ready || busy || Boolean(st.awaiting);
      pubBtn.disabled = !ready || Boolean(st.awaiting) || !editor.getValue().trim();
      pubBtn.lastChild.textContent = cur ? `Publish as R${(data.revisions[0]?.number ?? cur.number) + 1}` : "Publish";
      const checked = st.dry && st.dry.state === "succeeded" && st.dry.text === editor.getValue() && st.dry.base === cur?.revision_id;
      pubBtn.title = checked ? "Publishes the script you just dry-ran" : "Not dry-run in its current form; it is validated when published";
    }

    function summaryText() {
      return summary.value.trim() || "Edit from the browser editor";
    }

    dryBtn.addEventListener("click", () => act(dryBtn, async () => {
      const cur = current();
      if (!cur) return;
      const text = editor.getValue();
      const res = await api(`/api/projects/${projectId}/edits`, {
        method: "POST",
        body: { base_revision_id: cur.revision_id, source: text, summary: summaryText(), dry_run: true, strict_checks: strictBox.checked },
      });
      st.dry = { job_id: res.edit_job_id, text, base: cur.revision_id, strict: strictBox.checked, state: "queued", shown: null };
      renderResult();
      syncButtons();
      await load();
    }));

    pubBtn.addEventListener("click", () => act(pubBtn, async () => {
      const cur = current();
      if (!cur) return;
      const text = editor.getValue();
      const reuse = st.dry && st.dry.state === "succeeded" && st.dry.text === text && st.dry.base === cur.revision_id && st.dry.strict === strictBox.checked;
      const body = reuse
        ? { base_revision_id: cur.revision_id, from_dry_run: st.dry.job_id, summary: summaryText() }
        : { base_revision_id: cur.revision_id, source: text, summary: summaryText(), strict_checks: strictBox.checked };
      const res = await api(`/api/projects/${projectId}/edits`, { method: "POST", body });
      st.awaiting = { job_id: res.edit_job_id, text, state: "queued" };
      result.replaceChildren(h("p", { class: "stale-note" }, stateEl("running"), "Publishing: running, validating and rendering in Blender."));
      syncButtons();
      await load();
    }));

    revertBtn.addEventListener("click", () => {
      if (st.viewRev) show(st.viewRev, { animate: false });
    });

    select.addEventListener("change", () => {
      if (st.dirty && !window.confirm("Replace your unpublished changes with this revision's script?")) {
        select.value = st.viewRev || "";
        return;
      }
      show(select.value);
    });

    async function renderResult() {
      const dry = st.dry;
      if (!dry) return;
      if (ACTIVE.has(dry.state)) {
        result.replaceChildren(h("p", { class: "stale-note" }, stateEl("running"), "Dry run: running the script, checking protection and geometry, rendering."));
        return;
      }
      if (dry.shown === dry.state) return;
      dry.shown = dry.state;
      let job;
      try {
        job = await api(`/api/jobs/${dry.job_id}`);
      } catch (err) {
        result.replaceChildren(problem({ message: err.message }, "Could not load the dry run"));
        return;
      }
      if (job.state === "failed") {
        result.replaceChildren(problem(job.error || {}, "Dry run failed. Nothing was saved."));
        const line = job.error?.details?.line;
        if (line && editor.getValue() === dry.text) editor.markError(line);
        return;
      }
      const r = job.result || {};
      const images = (job.artifacts || []).filter((a) => a.kind === "render_image" && (a.view.endsWith("--highlight") || (a.view.startsWith("closeup_") && !a.view.includes("--"))));
      const checks = checkItems(r.checks);
      result.replaceChildren(
        h("div", { class: "code-ok" }, icon("shield-check"), h("b", {}, "Dry run passed. "), changeLine(r.changes), ". Not saved yet."),
        checks.length ? h("ul", { class: "checks" }, checks) : "",
        r.render_error ? problem(r.render_error, "The preview could not be rendered") : "",
        images.length ? h("div", { class: "code-previews" }, images.map((a) => h("figure", { class: "ref" },
          h("a", { href: a.url, target: "_blank", rel: "noopener" }, h("img", { src: a.url, alt: `${a.view} preview`, loading: "lazy" })),
          h("figcaption", {}, h("span", {}, a.view.replace("--highlight", " (changes)").replace("closeup_", "close-up "))))) ) : "",
        r.script_output ? h("details", { class: "report" }, h("summary", {}, "Script output"), h("pre", {}, r.script_output)) : "",
      );
    }

    function trackJobs() {
      const byId = Object.fromEntries(data.jobs.map((j) => [j.job_id, j]));
      if (st.dry) {
        const j = byId[st.dry.job_id];
        if (j && j.state !== st.dry.state) {
          st.dry.state = j.state;
          renderResult();
        }
      }
      if (st.awaiting) {
        const j = byId[st.awaiting.job_id];
        if (j && j.state === "failed") {
          result.replaceChildren(problem(j.error || {}, "Publishing failed. Nothing was saved."));
          const line = j.error?.details?.line;
          if (line && editor.getValue() === st.awaiting.text) editor.markError(line);
          st.awaiting = null;
        } else if (j && j.state === "succeeded") {
          st.awaiting.revision_id = j.revision_id;
        }
      }
    }

    function syncOptions() {
      const sig = data.revisions.map((r) => r.revision_id).join(",");
      if (sig === st.revSig) return;
      st.revSig = sig;
      select.replaceChildren(...data.revisions.map((r) => h("option", { value: r.revision_id },
        `R${r.number} · ${AUTHOR_LABEL[r.author] || r.author}${r.has_source ? "" : " (no script)"}`)));
      if (st.viewRev) select.value = st.viewRev;
    }

    return {
      el: section,
      sync() {
        if (!data) return;
        syncOptions();
        trackJobs();
        syncButtons();
        const cur = current();
        if (!cur) caption.textContent = "Code editing opens once the import finishes.";
        if (!cur || cur.revision_id === st.lastCurrent) return;
        const first = st.lastCurrent === null;
        st.lastCurrent = cur.revision_id;
        if (first) {
          const draft = readDraft();
          const draftRev = draft && revById(draft.rev) ? draft.rev : cur.revision_id;
          show(draftRev, { animate: !draft, text: draft?.text ?? null }).then(() => {
            if (draft && st.dirty) toast("Restored your unpublished draft.");
          });
          return;
        }
        const mine = st.awaiting && st.awaiting.revision_id === cur.revision_id;
        if (mine) {
          st.awaiting = null;
          result.replaceChildren(h("div", { class: "code-ok" }, icon("check"), h("b", {}, `Published as R${cur.number}.`), " The renders update when Blender finishes."));
          summary.value = "";
        }
        if (mine || !st.dirty) show(cur.revision_id);
        else showBanner(cur);
      },
      showRevision(revId) {
        if (st.dirty && revId !== st.viewRev && !window.confirm("Replace your unpublished changes with this revision's script?")) return;
        show(revId);
        section.scrollIntoView({ behavior: REDUCE_MOTION ? "auto" : "smooth", block: "start" });
      },
      refresh: () => editor.refresh(),
    };
  }
  const code = codePanel();
  codeSlot.append(code.el);
  code.refresh();

  let lastAnnounced = "";
  function announce() {
    const active = data.jobs.find((j) => ACTIVE.has(j.state));
    const msg = active ? `${jobLabel(active)} ${active.state}` : data.current_revision ? `Revision R${data.current_revision.number} is current` : "";
    if (msg !== lastAnnounced) {
      lastAnnounced = msg;
      live.textContent = msg;
    }
  }

  async function load() {
    try {
      data = await api(`/api/projects/${projectId}`);
    } catch (err) {
      if (err.status === 404) {
        main.replaceChildren(h("div", { class: "empty" }, h("strong", {}, "Project not found"), h("p", {}, "It may have been created on another data directory. "), h("a", { href: "/", "data-nav": true }, "Back to projects")));
        alive = false;
        return false;
      }
      throw err;
    }
    document.title = `${data.project.name} · Sculpt Workspace`;
    patch("head", head, [data.project, data.current_revision], renderHead);
    patch("renders", renders, [data.project.status, data.project.error, data.current_revision, data.renders], renderPlates);
    patch("brief", brief, [data.project.brief, data.project.references], renderBrief);
    patch("jobs", jobs, [data.jobs, data.jobs_total, jobsExpanded, history.length, data.revisions.length, data.jobs.some((j) => j.state === "running") ? Math.floor(Date.now() / 5000) : 0], renderJobs);
    patch("revs", revs, [data.revisions, data.current_revision?.revision_id], renderRevisions);
    try {
      code.sync();
    } catch (err) {
      console.error("Could not update the code panel", err);
    }
    announce();
    return data.jobs.some((j) => ACTIVE.has(j.state)) || data.project.status === "importing";
  }

  async function tick() {
    if (!alive) return;
    let busy = true;
    try {
      busy = await load();
    } catch (err) {
      toast(err.message);
    }
    if (busy) refreshEngine();
    if (alive) timer = setTimeout(tick, busy ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  const onFocus = () => { if (alive && !document.hidden) { clearTimeout(timer); tick(); } };
  document.addEventListener("visibilitychange", onFocus);
  tick();
  return {
    destroy() {
      alive = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onFocus);
    },
  };
}

/* ---------- routing ---------- */

function route() {
  screen?.destroy();
  const match = location.pathname.match(/^\/projects\/([A-Za-z0-9_-]{1,64})\/?$/);
  document.querySelectorAll("[data-nav-projects]").forEach((a) => {
    if (!match) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  screen = match ? workspaceScreen(match[1]) : projectsScreen();
}

function navigate(href) {
  if (href === location.pathname) return;
  history.pushState({}, "", href);
  route();
  main.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-nav]");
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
window.addEventListener("popstate", route);

refreshEngine().then(route);
setInterval(refreshEngine, 30000);
