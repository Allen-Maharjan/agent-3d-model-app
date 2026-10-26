"""Application services: the single entry point for browser and MCP operations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from .blender.discovery import BlenderStatus
from .blender.runner import Runner, UnavailableRunner
from .blender_scripts import sculpt_math
from .config import Settings
from .db import Database
from .errors import BlenderUnavailable, Conflict, InvalidRequest, NotFound, StaleRevision
from .executor import Executor, new_id
from .imaging import crop as crop_image
from .imaging import normalize_reference, overlay_annotator, preview, side_by_side
from .jobs import JobQueue
from .models import (
    PASS_SEPARATOR,
    STANDARD_VIEWS,
    BriefRequest,
    CheckoutRequest,
    EditRequest,
    ExportRequest,
    ImageRayQuery,
    InspectRequest,
    RenderRequest,
    ResultRequest,
    ViewFrame,
)
from .repositories import Repository
from .storage import Storage, sha256_file, write_json_atomic

log = logging.getLogger("sculpt.service")

BLEND_MAGICS = (b"BLENDER", b"\x28\xb5\x2f\xfd", b"\x1f\x8b")
DEFAULT_FRAME = ViewFrame().model_dump(mode="json")
MAX_RESULT_IMAGES = 8
MAX_OVERVIEW_REVISIONS = 50
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
WAIT_POLL_SECONDS = 0.25
MAX_REFERENCES = 8
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
# Extra passes on the automatic render after an edit, so the agent sees what changed.
EDIT_RENDER_PASSES = ["highlight"]


class Workspace:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        storage: Storage,
        runner: Runner,
        blender_status: BlenderStatus,
    ):
        self.settings = settings
        self.db = db
        self.repo = Repository(db)
        self.storage = storage
        self.runner = runner
        self.blender_status = blender_status
        self.queue = JobQueue(settings.queue_capacity)
        self.executor = Executor(settings, db, self.repo, storage, runner, self.queue)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        async with self.db.transaction() as conn:
            interrupted = await self.repo.fail_interrupted_jobs(conn)
        if interrupted:
            log.warning("recovered %d interrupted job(s) as failed", len(interrupted))
        cleared = self.storage.clear_staging()
        removed = self.storage.reconcile(
            await self.repo.all_project_ids(), await self.repo.all_revision_ids(), await self.repo.all_artifact_paths()
        )
        if cleared or removed:
            log.info("startup cleanup: staging=%d orphaned_files=%d", cleared, len(removed))
        self.queue.start(self.executor.execute)

    async def stop(self) -> None:
        await self.runner.shutdown()
        await self.queue.stop()

    def _require_blender(self) -> None:
        if isinstance(self.runner, UnavailableRunner):
            raise BlenderUnavailable(self.runner.reason)

    # -- projects -----------------------------------------------------------

    async def create_project(self, staged: Path, filename: str) -> dict[str, Any]:
        """Register an uploaded file (already streamed to `staged`) as a project."""
        self._require_blender()
        safe_name = os.path.basename(filename or "").strip()
        if not safe_name.lower().endswith(".blend"):
            raise InvalidRequest("Upload a .blend file.", code="invalid_file_type")
        size = staged.stat().st_size
        if size == 0:
            raise InvalidRequest("The uploaded file is empty.", code="invalid_blend_file")
        with staged.open("rb") as fh:
            head = fh.read(8)
        if not head.startswith(BLEND_MAGICS):
            raise InvalidRequest(
                "This is not a Blender file (the header is wrong). Re-save it from Blender and upload again.",
                code="invalid_blend_file",
            )
        project_id = str(uuid.uuid4())
        import_job, render_job = new_id("job"), new_id("job")
        digest = sha256_file(staged)
        self.queue.reserve(2)
        original = self.storage.original_path(project_id)
        try:
            self.storage.publish_file(staged, original)
            os.chmod(original, 0o400)
            async with self.db.transaction() as conn:
                await self.repo.insert_project(
                    conn,
                    {
                        "id": project_id,
                        "name": safe_name[: -len(".blend")][:120] or "Untitled",
                        "original_filename": safe_name[:200],
                        "original_size": size,
                        "original_sha256": digest,
                    },
                )
                await self.repo.insert_job(
                    conn,
                    {
                        "id": import_job,
                        "project_id": project_id,
                        "kind": "import",
                        "state": "queued",
                        "params": {"render_job_id": render_job},
                    },
                )
                await self.repo.insert_job(
                    conn,
                    {
                        "id": render_job,
                        "project_id": project_id,
                        "kind": "render",
                        "state": "blocked",
                        "parent_job_id": import_job,
                        "params": self._standard_render_params(DEFAULT_FRAME),
                    },
                )
        except BaseException:
            self.queue.release(2)
            self.storage.unpublish(self.storage.project_dir(project_id))
            raise
        self.queue.put(import_job)
        log.info("project created project_id=%s job_id=%s size=%d", project_id, import_job, size)
        return {"project_id": project_id, "import_job_id": import_job, "render_job_id": render_job}

    def _standard_render_params(self, frame: dict[str, Any], edit: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {
            "views": list(STANDARD_VIEWS),
            "cameras": [],
            "engine": "workbench",
            "resolution": self.settings.standard_resolution,
            "frame": frame,
            "standard": True,
        }
        if edit:
            # Highlight what changed and add close-ups of the new or changed objects.
            params["passes"] = list(EDIT_RENDER_PASSES)
            params["auto_closeups"] = True
        return params

    async def list_projects(self) -> list[dict[str, Any]]:
        return [self._project_summary(p) for p in await self.repo.list_projects()]

    @staticmethod
    def _project_summary(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_id": p["id"],
            "name": p["name"],
            "original_filename": p["original_filename"],
            "original_size": p["original_size"],
            "status": p["status"],
            "error": p["error"],
            "current_revision_id": p["current_revision_id"],
            "current_revision_number": p.get("current_revision_number"),
            "revision_count": p.get("revision_count", 0),
            "created_at": p["created_at"],
            "updated_at": p["updated_at"],
        }

    async def overview(self, project_id: str | None) -> dict[str, Any]:
        """What an agent needs to start: ids, history, latest render and the brief.

        Without a project id, lists the projects instead.
        """
        if project_id is None:
            return {
                "projects": [
                    {
                        k: p[k]
                        for k in ("project_id", "name", "status", "current_revision_id", "revision_count", "updated_at")
                    }
                    for p in await self.list_projects()
                ],
                "next": "Call get_project(project_id) for the project you were asked to work on.",
            }
        project = await self._project(project_id)
        revisions = await self.repo.list_revisions(project_id)
        current = project["current_revision_id"]
        latest = await self.repo.latest_standard_render(current, succeeded_only=True) if current else None
        pending = await self.repo.latest_standard_render(current) if current else None
        active = [j for j in await self.repo.list_jobs(project_id) if j["state"] not in TERMINAL_STATES]
        history = []
        for r in revisions[:MAX_OVERVIEW_REVISIONS]:
            report = r["protection_report"] or {}
            history.append(
                {
                    "revision_id": r["id"],
                    "number": r["number"],
                    "parent_id": r["parent_id"],
                    "current": r["id"] == current,
                    "summary": r["summary"],
                    "created_at": r["created_at"],
                    "edit_job_id": r["job_id"] if r["number"] > 1 else None,
                    "agent_objects": len(report.get("agent_objects") or []),
                    "changes": report.get("changes"),
                }
            )
        views = []
        if latest is not None:
            views = [a["view"] for a in await self.repo.job_artifacts(latest["id"]) if a["kind"] == "render_image"]
        return {
            "project_id": project_id,
            "name": project["name"],
            "status": project["status"],
            "error": project["error"],
            "brief": project.get("brief") or "",
            "references": [
                {"reference_id": r["reference_id"], "note": r["note"], "filename": r["filename"]}
                for r in self._reference_index(project_id)
            ],
            "view_frame": project["view_frame"] or DEFAULT_FRAME,
            "current_revision_id": current,
            "revisions": history,
            "revisions_omitted": max(0, len(revisions) - MAX_OVERVIEW_REVISIONS),
            "latest_render": {"job_id": latest["id"], "views": views} if latest else None,
            "render_in_progress": (
                pending["id"] if pending is not None and pending["state"] not in TERMINAL_STATES else None
            ),
            "active_jobs": [{"job_id": j["id"], "kind": j["kind"], "state": j["state"]} for j in active],
            "next": "inspect_project(project_id) for objects and units; get_result(latest_render.job_id) to see "
            "the current renders; get_result(edit_job_id, include_source=true, include_images=false) to read "
            "the script behind a revision.",
        }

    async def _project(self, project_id: str) -> dict[str, Any]:
        project = await self.repo.get_project(project_id)
        if project is None:
            raise NotFound("Project not found", details={"project_id": project_id})
        return project

    async def _revision(self, project_id: str, revision_id: str) -> dict[str, Any]:
        rev = await self.repo.get_revision(revision_id)
        if rev is None or rev["project_id"] != project_id:
            raise NotFound("Revision not found in this project", details={"revision_id": revision_id})
        return rev

    async def project_detail(self, project_id: str) -> dict[str, Any]:
        project = await self._project(project_id)
        revisions = await self.repo.list_revisions(project_id)
        current = project["current_revision_id"]
        renders: dict[str, Any] | None = None
        if current:
            latest = await self.repo.latest_standard_render(current)
            shown = latest
            if latest is not None and latest["state"] != "succeeded":
                shown = await self.repo.latest_standard_render(current, succeeded_only=True)
            renders = {
                "latest_job": self._job_brief(latest) if latest else None,
                "images": await self._artifact_list(shown["id"]) if shown else [],
                "shown_job_id": shown["id"] if shown else None,
            }
        jobs = await self.repo.list_jobs(project_id)
        _, jobs_total = await self.repo.job_history(project_id, 0, 0)
        return {
            "project": {
                **self._project_summary({**project, "revision_count": len(revisions)}),
                "view_frame": project["view_frame"] or DEFAULT_FRAME,
                "original_sha256": project["original_sha256"],
                "brief": project.get("brief") or "",
                "references": self.list_references(project_id),
            },
            "current_revision": next((self._revision_brief(r) for r in revisions if r["id"] == current), None),
            "revisions": [self._revision_brief(r) for r in revisions],
            "renders": renders,
            "jobs": [self._job_brief(j) for j in jobs],
            "jobs_total": jobs_total,
        }

    async def job_history(self, project_id: str, offset: int, limit: int) -> dict[str, Any]:
        await self._project(project_id)
        jobs, total = await self.repo.job_history(project_id, offset, limit)
        return {"jobs": [self._job_brief(j) for j in jobs], "total": total, "offset": offset}

    @staticmethod
    def _revision_brief(r: dict[str, Any]) -> dict[str, Any]:
        report = r["protection_report"] or {}
        return {
            "revision_id": r["id"],
            "number": r["number"],
            "parent_id": r["parent_id"],
            "summary": r["summary"],
            "created_at": r["created_at"],
            "blender_version": r["blender_version"],
            "protection": {
                "ok": report.get("ok"),
                "baseline": report.get("baseline", False),
                "protected_objects": report.get("protected_objects"),
                "violations": report.get("violations", []),
                "agent_objects": report.get("agent_objects", []),
                "changes": report.get("changes"),
                "checks": report.get("checks", []),
            },
            "job_id": r["job_id"],
        }

    @staticmethod
    def _job_brief(j: dict[str, Any]) -> dict[str, Any]:
        params = dict(j["params"] or {})
        params.pop("source", None)
        return {
            "job_id": j["id"],
            "project_id": j["project_id"],
            "kind": j["kind"],
            "state": j["state"],
            "base_revision_id": j["base_revision_id"],
            "revision_id": j["revision_id"],
            "parent_job_id": j["parent_job_id"],
            "params": params,
            "result": j["result"],
            "error": j["error"],
            "created_at": j["created_at"],
            "started_at": j["started_at"],
            "finished_at": j["finished_at"],
        }

    async def _artifact_list(self, job_id: str) -> list[dict[str, Any]]:
        return [self.artifact_public(a) for a in await self.repo.job_artifacts(job_id)]

    def artifact_public(self, a: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": a["id"],
            "kind": a["kind"],
            "view": a["view"],
            "revision_id": a["revision_id"],
            "job_id": a["job_id"],
            "mime": a["mime"],
            "size": a["size"],
            "sha256": a["sha256"],
            "url": f"/api/artifacts/{a['id']}",
            "path": str(self.storage.root / a["rel_path"]),
            "metadata": a["metadata"],
        }

    # -- mutations ----------------------------------------------------------

    async def submit_edit(self, req: EditRequest) -> dict[str, Any]:
        self._require_blender()
        if req.from_dry_run is not None:
            req = await self._from_dry_run(req)
        if req.dry_run:
            return await self._submit_dry_run(req)
        async with self._locks[req.project_id]:
            project = await self._project(req.project_id)
            await self._revision(req.project_id, req.base_revision_id)
            if project["status"] != "ready":
                raise Conflict("The project has not finished importing.", code="project_not_ready")
            edit_job, render_job = new_id("job"), new_id("job")
            frame = project["view_frame"] or DEFAULT_FRAME
            self.queue.reserve(2)
            try:
                async with self.db.transaction() as conn:
                    pending = await self.repo.pending_mutations(conn, req.project_id)
                    if pending:
                        raise Conflict(
                            "Another edit or import for this project is still pending.",
                            code="mutation_pending",
                            details={"pending_job_ids": pending},
                        )
                    fresh = await self.repo.get_project_conn(conn, req.project_id)
                    if fresh["current_revision_id"] != req.base_revision_id:
                        raise StaleRevision(
                            "base_revision_id is not the current revision. Inspect the current revision and retry.",
                            details={
                                "base_revision_id": req.base_revision_id,
                                "current_revision_id": fresh["current_revision_id"],
                            },
                        )
                    await self.repo.insert_job(
                        conn,
                        {
                            "id": edit_job,
                            "project_id": req.project_id,
                            "kind": "edit",
                            "state": "queued",
                            "base_revision_id": req.base_revision_id,
                            "params": {
                                "source": req.source,
                                "summary": req.summary,
                                "render_job_id": render_job,
                                "strict_checks": req.strict_checks,
                            },
                        },
                    )
                    await self.repo.insert_job(
                        conn,
                        {
                            "id": render_job,
                            "project_id": req.project_id,
                            "kind": "render",
                            "state": "blocked",
                            "parent_job_id": edit_job,
                            "params": self._standard_render_params(frame, edit=True),
                        },
                    )
            except BaseException:
                self.queue.release(2)
                raise
        self.queue.put(edit_job)
        return {"edit_job_id": edit_job, "render_job_id": render_job, "base_revision_id": req.base_revision_id}

    async def _from_dry_run(self, req: EditRequest) -> EditRequest:
        """Turn `from_dry_run` into an ordinary edit carrying the dry run's script.

        The script is re-run and re-validated against the same base, so the
        published revision is checked exactly like any other edit.
        """
        job = await self.repo.get_job(req.from_dry_run or "")
        params = (job or {}).get("params") or {}
        if job is None or job["project_id"] != req.project_id or job["kind"] != "edit" or not params.get("dry_run"):
            raise NotFound("Dry run not found in this project", details={"from_dry_run": req.from_dry_run})
        if job["state"] != "succeeded":
            raise Conflict(
                "Only a succeeded dry run can be published.",
                code="dry_run_not_succeeded",
                details={"from_dry_run": job["id"], "state": job["state"]},
            )
        if job["base_revision_id"] != req.base_revision_id:
            raise Conflict(
                "The dry run was made against a different base revision. Dry-run the script again on this one.",
                code="dry_run_base_mismatch",
                details={"dry_run_base_revision_id": job["base_revision_id"], "base_revision_id": req.base_revision_id},
            )
        return req.model_copy(
            update={
                "source": params["source"],
                "summary": req.summary or params["summary"],
                "strict_checks": req.strict_checks or bool(params.get("strict_checks")),
                "from_dry_run": None,
            }
        )

    async def _submit_dry_run(self, req: EditRequest) -> dict[str, Any]:
        """Queue an edit that is validated and rendered but never published.

        It may target any revision and does not block, or wait for, real edits.
        """
        project = await self._project(req.project_id)
        await self._revision(req.project_id, req.base_revision_id)
        if project["status"] != "ready":
            raise Conflict("The project has not finished importing.", code="project_not_ready")
        job_id = new_id("job")
        frame = project["view_frame"] or DEFAULT_FRAME
        self.queue.reserve(1)
        try:
            async with self.db.transaction() as conn:
                await self.repo.insert_job(
                    conn,
                    {
                        "id": job_id,
                        "project_id": req.project_id,
                        "kind": "edit",
                        "state": "queued",
                        "base_revision_id": req.base_revision_id,
                        "params": {
                            "source": req.source,
                            "summary": req.summary,
                            "dry_run": True,
                            "strict_checks": req.strict_checks,
                            "render": self._standard_render_params(frame, edit=True),
                        },
                    },
                )
        except BaseException:
            self.queue.release(1)
            raise
        self.queue.put(job_id)
        return {"edit_job_id": job_id, "dry_run": True, "base_revision_id": req.base_revision_id}

    async def checkout(self, req: CheckoutRequest) -> dict[str, Any]:
        async with self._locks[req.project_id]:
            await self._project(req.project_id)
            target = await self._revision(req.project_id, req.revision_id)
            async with self.db.transaction() as conn:
                pending = await self.repo.pending_mutations(conn, req.project_id)
                if pending:
                    raise Conflict(
                        "Wait for the pending edit or import to finish before checking out.",
                        code="mutation_pending",
                        details={"pending_job_ids": pending},
                    )
                moved = await self.repo.move_current_revision(
                    conn, req.project_id, req.expected_current_revision_id, req.revision_id
                )
                if not moved:
                    fresh = await self.repo.get_project_conn(conn, req.project_id)
                    raise StaleRevision(
                        "expected_current_revision_id does not match the current revision.",
                        details={"current_revision_id": fresh["current_revision_id"]},
                    )
        return {"project_id": req.project_id, "current_revision_id": req.revision_id, "number": target["number"]}

    async def render(self, req: RenderRequest) -> dict[str, Any]:
        self._require_blender()
        project = await self._project(req.project_id)
        revision_id = req.revision_id or project["current_revision_id"]
        if not revision_id:
            raise Conflict("The project has no revision yet.", code="project_not_ready")
        await self._revision(req.project_id, revision_id)
        frame = req.frame.model_dump(mode="json") if req.frame else (project["view_frame"] or DEFAULT_FRAME)
        views = list(req.views) if req.views else ([] if req.cameras else list(STANDARD_VIEWS))
        cameras = [c.model_dump(mode="json") for c in req.cameras or []]
        closeups = None
        if req.focus_object_ids:
            # Close-ups replace the whole-scene views: same angles, framed on the objects.
            closeups = {"object_ids": list(req.focus_object_ids), "views": views}
            views = []
        params = {
            "views": views,
            "cameras": cameras,
            "engine": req.engine,
            "resolution": req.resolution,
            "frame": frame,
            "standard": set(STANDARD_VIEWS) <= set(views),
            "passes": list(req.passes or []),
            "closeups": closeups,
        }
        job_id = new_id("job")
        self.queue.reserve(1)
        try:
            async with self.db.transaction() as conn:
                if req.persist_frame and req.frame is not None:
                    await self.repo.set_view_frame(conn, req.project_id, frame)
                await self.repo.insert_job(
                    conn,
                    {
                        "id": job_id,
                        "project_id": req.project_id,
                        "kind": "render",
                        "state": "queued",
                        "revision_id": revision_id,
                        "params": params,
                    },
                )
        except BaseException:
            self.queue.release(1)
            raise
        self.queue.put(job_id)
        return {"job_id": job_id, "revision_id": revision_id, "frame": frame, "frame_persisted": req.persist_frame}

    async def inspect(self, req: InspectRequest) -> dict[str, Any]:
        project = await self._project(req.project_id)
        if req.revision_id is None:
            if not project["current_revision_id"]:
                raise Conflict("The project has no revision yet.", code="project_not_ready")
            req = req.model_copy(update={"revision_id": project["current_revision_id"]})
        assert req.revision_id is not None
        await self._revision(req.project_id, req.revision_id)
        if req.query is None:
            path = self.storage.revision_inspection(req.project_id, req.revision_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            baseline = json.loads(self.storage.baseline_path(req.project_id).read_text(encoding="utf-8"))
            for obj in data.get("objects", []):
                obj["protected"] = obj.get("id") in baseline
            return {
                "cached": True,
                "revision_id": req.revision_id,
                "brief": project.get("brief") or "",
                "references": self.list_references(req.project_id),
                "inspection": data,
            }
        self._require_blender()
        query = req.query.model_dump(mode="json")
        params: dict[str, Any] = {"query": query}
        if isinstance(req.query, ImageRayQuery):
            params["derived_ray"] = await self._image_ray(req.project_id, req.revision_id, req.query)
            params["query"] = {
                "type": "image_ray",
                "origin": params["derived_ray"]["origin"],
                "direction": params["derived_ray"]["direction"],
                "max_distance": params["derived_ray"]["max_distance"],
                "object_ids": query.get("object_ids"),
                "artifact_id": req.query.artifact_id,
                "u": req.query.u,
                "v": req.query.v,
            }
        job_id = new_id("job")
        self.queue.reserve(1)
        try:
            async with self.db.transaction() as conn:
                await self.repo.insert_job(
                    conn,
                    {
                        "id": job_id,
                        "project_id": req.project_id,
                        "kind": "inspect",
                        "state": "queued",
                        "revision_id": req.revision_id,
                        "params": params,
                    },
                )
        except BaseException:
            self.queue.release(1)
            raise
        self.queue.put(job_id)
        return {"cached": False, "job_id": job_id, "revision_id": req.revision_id}

    async def _image_ray(self, project_id: str, revision_id: str, q: ImageRayQuery) -> dict[str, Any]:
        art = await self.repo.get_artifact(q.artifact_id)
        if art is None or art["project_id"] != project_id or art["kind"] != "render_image":
            raise NotFound("Render image not found", details={"artifact_id": q.artifact_id})
        if art["revision_id"] != revision_id:
            raise InvalidRequest(
                "The render belongs to a different revision; image probes are revision-specific.",
                details={"artifact_revision_id": art["revision_id"], "revision_id": revision_id},
            )
        camera = art["metadata"]["camera"]
        ray = sculpt_math.image_ray(camera, q.u, q.v)
        return {**ray, "u": q.u, "v": q.v, "image_origin": "top-left", "view": art["view"]}

    async def export(self, req: ExportRequest) -> dict[str, Any]:
        await self._project(req.project_id)
        rev = await self._revision(req.project_id, req.revision_id)
        if req.format == "blend":
            path = self.storage.revision_model(req.project_id, req.revision_id)
            return {
                "cached": True,
                "artifact": {
                    "kind": "export_blend",
                    "revision_id": req.revision_id,
                    "revision_number": rev["number"],
                    "mime": "application/x-blender",
                    "size": path.stat().st_size,
                    "url": f"/api/projects/{req.project_id}/revisions/{req.revision_id}/model.blend",
                    "path": str(path),
                    "note": "Self-contained .blend with packed assets; identical to the stored revision.",
                },
            }
        self._require_blender()
        job_id = new_id("job")
        self.queue.reserve(1)
        try:
            async with self.db.transaction() as conn:
                await self.repo.insert_job(
                    conn,
                    {
                        "id": job_id,
                        "project_id": req.project_id,
                        "kind": "export",
                        "state": "queued",
                        "revision_id": req.revision_id,
                        "params": {"stl_units": req.stl_units},
                    },
                )
        except BaseException:
            self.queue.release(1)
            raise
        self.queue.put(job_id)
        return {"cached": False, "job_id": job_id, "revision_id": req.revision_id}

    # -- results ------------------------------------------------------------

    async def job(self, job_id: str) -> dict[str, Any]:
        job = await self.repo.get_job(job_id)
        if job is None:
            raise NotFound("Job not found", details={"job_id": job_id})
        out = self._job_brief(job)
        out["artifacts"] = await self._artifact_list(job_id)
        return out

    async def result(self, req: ResultRequest) -> tuple[dict[str, Any], list[tuple[bytes, dict[str, Any]]]]:
        """Job state plus bounded image payloads for MCP."""
        info, images = await self._result(req)
        return (self._compact(info) if req.detail == "compact" else info), images

    async def _wait(self, job_id: str, seconds: float) -> tuple[float, bool]:
        """Wait until the job, and the render an edit or import queued, has finished."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            job = await self.repo.get_job(job_id)
            if job is None:
                raise NotFound("Job not found", details={"job_id": job_id})
            done = job["state"] in TERMINAL_STATES
            child = (job["result"] or {}).get("render_job_id") if job["state"] == "succeeded" else None
            if done and child:
                render = await self.repo.get_job(child)
                done = render is None or render["state"] in TERMINAL_STATES
            waited = loop.time() - start
            if done or waited >= seconds:
                return round(waited, 2), done
            await asyncio.sleep(min(WAIT_POLL_SECONDS, seconds - waited))

    @staticmethod
    def _compact_artifacts(arts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"artifact_id": a["artifact_id"], "kind": a["kind"], "view": a["view"]} for a in arts]

    def _compact(self, info: dict[str, Any]) -> dict[str, Any]:
        """Drop what an agent never needs: camera matrices, duplicated lists, render params.

        Image probes and crops take artifact ids, so the camera data stays server-side.
        """
        out = dict(info)
        out["artifacts"] = self._compact_artifacts(info.get("artifacts") or [])
        params = dict(out.get("params") or {})
        params.pop("render", None)
        params.pop("cameras", None)
        out["params"] = params
        result = dict(out.get("result") or {}) if out.get("result") else out.get("result")
        if isinstance(result, dict):
            result.pop("images", None)
            report = result.get("protection_report")
            if isinstance(report, dict):
                result["protection_report"] = {
                    k: report.get(k) for k in ("ok", "protected_objects", "violations") if k in report
                }
            out["result"] = result
        render = out.get("render_job")
        if isinstance(render, dict):
            render = dict(render)
            render["artifacts"] = self._compact_artifacts(render.get("artifacts") or [])
            if isinstance(render.get("result"), dict):
                render["result"] = {k: v for k, v in render["result"].items() if k != "images"}
            out["render_job"] = render
        out["detail"] = "compact; pass detail='full' for artifact metadata (cameras, bounds)"
        return out

    async def _result(self, req: ResultRequest) -> tuple[dict[str, Any], list[tuple[bytes, dict[str, Any]]]]:
        waited: tuple[float, bool] | None = None
        if req.wait_seconds > 0:
            waited = await self._wait(req.job_id, req.wait_seconds)
        info = await self.job(req.job_id)
        if waited is not None:
            info["waited_seconds"], finished = waited
            info["still_running"] = not finished
        if req.include_source and info["kind"] == "edit":
            raw = await self.repo.get_job(req.job_id)
            info["source"] = ((raw or {}).get("params") or {}).get("source")
        images: list[tuple[bytes, dict[str, Any]]] = []
        source_job = info
        if info["kind"] in ("edit", "import") and info["result"]:
            child = (info["result"] or {}).get("render_job_id")
            if child:
                source_job = await self.job(child)
                info["render_job"] = {k: source_job[k] for k in ("job_id", "state", "error", "result")}
                info["render_job"]["artifacts"] = source_job["artifacts"]
        arts = [a for a in source_job["artifacts"] if a["kind"] == "render_image"]
        if not req.include_images or source_job["state"] != "succeeded" or not arts:
            return info, images
        by_view = {a["view"]: a for a in arts}
        scale_length = self._scale_length(source_job) if req.overlays else None

        def annotate(a: dict[str, Any]) -> Any:
            return overlay_annotator(a["metadata"], scale_length) if req.overlays else None

        if req.crop is not None:
            match = by_view.get(req.crop.view)
            if match is None:
                raise NotFound("No rendered view with that name", details={"view": req.crop.view})
            data, size, box = crop_image(
                self.storage.resolve_artifact(self._rel(match)), req.crop, req.max_image_size, annotate(match)
            )
            images.append(
                (data, {"artifact_id": match["artifact_id"], "view": match["view"], "crop_pixels": box, "size": size})
            )
            return info, images
        base_views = req.views or [a["view"] for a in arts if PASS_SEPARATOR not in a["view"]]
        wanted = [view if p == "color" else f"{view}{PASS_SEPARATOR}{p}" for view in base_views for p in req.passes]
        before = await self._before_images(info, source_job) if req.compare else None
        if req.compare and before is None:
            info["compare_note"] = "No earlier revision render to compare with."
        missing = []
        for view in wanted:
            match = by_view.get(view)
            if match is None:
                missing.append(view)
                continue
            if len(images) >= MAX_RESULT_IMAGES:
                info["images_omitted"] = info.get("images_omitted", 0) + 1
                continue
            path = self.storage.resolve_artifact(self._rel(match))
            meta = {"artifact_id": match["artifact_id"], "view": view}
            prior = (before or {}).get(view)
            if prior is not None:
                data, size = side_by_side(
                    self.storage.resolve_artifact(self._rel(prior)),
                    path,
                    req.max_image_size,
                    (f"before: {prior['label']}", "after"),
                    annotate(match),
                )
                meta["compared_with_artifact_id"] = prior["artifact_id"]
            else:
                data, size = preview(path, req.max_image_size, annotate(match))
            images.append((data, {**meta, "size": size}))
        if missing and req.views:
            info["missing_views"] = missing
        return info, images

    async def _before_images(self, info: dict[str, Any], source_job: dict[str, Any]) -> dict[str, Any] | None:
        """The previous revision's standard render, by view name, for before/after comparisons."""
        after_revision = source_job.get("revision_id")
        if after_revision:
            rev = await self.repo.get_revision(after_revision)
            parent = rev["parent_id"] if rev else None
        else:
            parent = info.get("base_revision_id")  # a dry run compares against its base
        if not parent:
            return None
        job = await self.repo.latest_standard_render(parent, succeeded_only=True)
        if job is None:
            return None
        rev = await self.repo.get_revision(parent)
        label = f"R{rev['number']}" if rev else parent
        return {
            a["view"]: {**a, "label": label}
            for a in await self._artifact_list(job["id"])
            if a["kind"] == "render_image"
        }

    def _scale_length(self, job: dict[str, Any]) -> float | None:
        rid = job.get("revision_id") or job.get("base_revision_id")
        if not rid:
            return None
        try:
            data = json.loads(self.storage.revision_inspection(job["project_id"], rid).read_text(encoding="utf-8"))
            return float(data["scene"]["units"]["scale_length"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # -- brief and references -------------------------------------------------

    async def set_brief(self, req: BriefRequest) -> dict[str, Any]:
        await self._project(req.project_id)
        brief = req.brief.strip()
        async with self.db.transaction() as conn:
            await self.repo.set_brief(conn, req.project_id, brief or None)
        return {"project_id": req.project_id, "brief": brief}

    def _reference_index(self, project_id: str) -> list[dict[str, Any]]:
        path = self.storage.references_dir(project_id) / "index.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def list_references(self, project_id: str) -> list[dict[str, Any]]:
        return [
            {**r, "url": f"/api/projects/{project_id}/references/{r['reference_id']}"}
            for r in self._reference_index(project_id)
        ]

    async def add_reference(self, project_id: str, data: bytes, filename: str, note: str) -> dict[str, Any]:
        await self._project(project_id)
        png, size = normalize_reference(data)
        async with self._locks[project_id]:
            index = self._reference_index(project_id)
            if len(index) >= MAX_REFERENCES:
                raise Conflict(
                    f"A project can hold {MAX_REFERENCES} reference images. Remove one first.",
                    code="too_many_references",
                )
            ref_id = new_id("ref")
            folder = self.storage.references_dir(project_id)
            folder.mkdir(parents=True, exist_ok=True)
            staged = folder / f".{ref_id}.tmp"
            staged.write_bytes(png)
            self.storage.publish_file(staged, self.storage.reference_path(project_id, ref_id))
            entry = {
                "reference_id": ref_id,
                "filename": os.path.basename(filename or "")[:200] or "reference",
                "note": note.strip()[:500],
                "width": size[0],
                "height": size[1],
                "size": len(png),
            }
            write_json_atomic(folder / "index.json", [*index, entry])
        return {**entry, "url": f"/api/projects/{project_id}/references/{ref_id}"}

    async def delete_reference(self, project_id: str, reference_id: str) -> dict[str, Any]:
        await self._project(project_id)
        async with self._locks[project_id]:
            index = self._reference_index(project_id)
            kept = [r for r in index if r["reference_id"] != reference_id]
            if len(kept) == len(index):
                raise NotFound("Reference image not found", details={"reference_id": reference_id})
            write_json_atomic(self.storage.references_dir(project_id) / "index.json", kept)
            self.storage.unpublish(self.storage.reference_path(project_id, reference_id))
        return {"project_id": project_id, "deleted": reference_id}

    async def reference_file(self, project_id: str, reference_id: str) -> Path:
        await self._project(project_id)
        if not any(r["reference_id"] == reference_id for r in self._reference_index(project_id)):
            raise NotFound("Reference image not found", details={"reference_id": reference_id})
        path = self.storage.reference_path(project_id, reference_id)
        if not path.is_file():
            raise NotFound("Reference image is unavailable")
        return path

    async def reference_images(self, project_id: str, max_size: int = 768) -> list[tuple[bytes, dict[str, Any]]]:
        out = []
        for ref in self._reference_index(project_id):
            path = self.storage.reference_path(project_id, ref["reference_id"])
            if path.is_file():
                data, size = preview(path, max_size)
                out.append((data, {"reference_id": ref["reference_id"], "note": ref["note"], "size": size}))
        return out

    def _rel(self, artifact: dict[str, Any]) -> str:
        return Path(artifact["path"]).relative_to(self.storage.root).as_posix()

    async def artifact_file(self, artifact_id: str) -> tuple[Path, dict[str, Any]]:
        art = await self.repo.get_artifact(artifact_id)
        if art is None:
            raise NotFound("Artifact not found", details={"artifact_id": artifact_id})
        return self.storage.resolve_artifact(art["rel_path"]), art

    async def revision_file(self, project_id: str, revision_id: str) -> tuple[Path, dict[str, Any]]:
        project = await self._project(project_id)
        rev = await self._revision(project_id, revision_id)
        path = self.storage.revision_model(project_id, revision_id)
        if not path.is_file():
            raise NotFound("Revision file is unavailable")
        return path, {"project": project, "revision": rev}
