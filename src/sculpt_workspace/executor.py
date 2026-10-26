"""Job handlers: run Blender, validate outputs, and publish atomically.

Publication order is always: stage -> validate -> rename into place -> commit
metadata. If the commit fails the renamed files are removed again, so a
revision is never visible without its files, nor files without a revision.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from .blender.runner import Runner
from .config import Settings
from .db import Database, utcnow
from .errors import PublicationError, SculptError, StaleRevision
from .imaging import validate_png
from .jobs import JobQueue
from .protection import changed_ids, compare, diff_revisions
from .repositories import Repository
from .storage import Storage, sha256_file, write_json_atomic

log = logging.getLogger("sculpt.jobs")

MAX_INSPECT_RESULT_BYTES = 2_000_000
AUTO_CLOSEUP_VIEWS = ("front", "perspective")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Executor:
    def __init__(
        self, settings: Settings, db: Database, repo: Repository, storage: Storage, runner: Runner, queue: JobQueue
    ):
        self.settings = settings
        self.db = db
        self.repo = repo
        self.storage = storage
        self.runner = runner
        self.queue = queue

    # -- dispatch -----------------------------------------------------------

    async def execute(self, job_id: str) -> None:
        job = await self.repo.get_job(job_id)
        if job is None or job["state"] != "queued":
            return
        async with self.db.transaction() as conn:
            await self.repo.update_job(conn, job_id, state="running", started_at=utcnow())
        started = time.monotonic()
        handler = getattr(self, f"_run_{job['kind']}")
        outcome = "succeeded"
        try:
            await handler(job)
        except SculptError as exc:
            outcome = f"failed:{exc.code}"
            await self._fail(job, exc.to_dict())
        except Exception as exc:
            log.exception("unexpected job error job_id=%s", job_id)
            outcome = "failed:internal_error"
            await self._fail(job, {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}", "details": {}})
        finally:
            self.storage.discard_staging(job_id)
            log.info(
                "job finished job_id=%s kind=%s outcome=%s duration=%.2fs",
                job_id,
                job["kind"],
                outcome,
                time.monotonic() - started,
            )

    async def _fail(self, job: dict[str, Any], error: dict[str, Any]) -> None:
        children = []
        async with self.db.transaction() as conn:
            await self.repo.update_job(conn, job["id"], state="failed", error=error, finished_at=utcnow())
            if job["kind"] in ("import", "edit"):
                child = job["params"].get("render_job_id")
                if child:
                    await self.repo.update_job(
                        conn,
                        child,
                        state="cancelled",
                        error={
                            "code": f"{job['kind']}_failed",
                            "message": f"Not rendered because the {job['kind']} job failed.",
                            "details": {"job_id": job["id"]},
                        },
                        finished_at=utcnow(),
                    )
                    children.append(child)
            if job["kind"] == "import":
                await self.repo.set_project_status(conn, job["project_id"], "import_failed", error)
        self.queue.release(len(children))

    async def _blender(self, script: str, request: dict[str, Any], job_id: str, step: str, deadline: float) -> dict:
        work = self.storage.staging / job_id / step
        work.mkdir(parents=True, exist_ok=True)
        log_path = self.storage.logs / f"{job_id}.{step}.log"
        return await self.runner.run(script, request, workdir=work, deadline=deadline, log_path=log_path)

    # -- import -------------------------------------------------------------

    async def _run_import(self, job: dict[str, Any]) -> None:
        pid, job_id = job["project_id"], job["id"]
        project = await self.repo.get_project(pid)
        assert project is not None
        work = self.storage.new_staging(job_id)
        out_dir = work / "revision"
        out_dir.mkdir()
        imported = await self._blender(
            "import_project.py",
            {"original": str(self.storage.original_path(pid)), "output_dir": str(out_dir)},
            job_id,
            "import",
            self.settings.deadlines.import_,
        )
        baseline = imported["fingerprints"]
        checked = await self._blender(
            "validate_candidate.py",
            {"candidate": str(out_dir / "model.blend")},
            job_id,
            "validate",
            self.settings.deadlines.validate,
        )
        self._require_self_contained(checked)
        report = compare(baseline, checked["fingerprints"])
        if not report["ok"] or report["agent_objects"]:
            raise SculptError(
                "The protection baseline could not be reproduced after saving, so edits could not be "
                "validated reliably.",
                code="baseline_not_reproducible",
                details={"violations": report["violations"]},
            )
        write_json_atomic(out_dir / "inspection.json", checked["inspection"])
        write_json_atomic(out_dir / "fingerprints.json", checked["fingerprints"])
        staged_baseline = work / "baseline.json"
        write_json_atomic(staged_baseline, baseline)

        revision_id = new_id("rev")
        rev_dir = self.storage.revision_dir(pid, revision_id)
        baseline_path = self.storage.baseline_path(pid)
        render_job_id = job["params"]["render_job_id"]
        protection = {**report, "baseline": True, "agent_objects": []}
        self.storage.publish_dir(out_dir, rev_dir)
        try:
            self.storage.publish_file(staged_baseline, baseline_path)
            async with self.db.transaction() as conn:
                await self.repo.insert_revision(
                    conn,
                    {
                        "id": revision_id,
                        "project_id": pid,
                        "number": 1,
                        "parent_id": None,
                        "summary": f"Imported {project['original_filename']}",
                        "created_at": utcnow(),
                        "blender_version": self.runner.version,
                        "protection_report": protection,
                        "job_id": job_id,
                        "author": "upload",
                    },
                )
                if not await self.repo.move_current_revision(conn, pid, None, revision_id):
                    raise StaleRevision("Project already has a revision")
                await self.repo.set_project_status(conn, pid, "ready")
                await self.repo.update_job(conn, render_job_id, state="queued", revision_id=revision_id)
                await self.repo.update_job(
                    conn,
                    job_id,
                    state="succeeded",
                    revision_id=revision_id,
                    finished_at=utcnow(),
                    result={
                        "revision_id": revision_id,
                        "revision_number": 1,
                        "render_job_id": render_job_id,
                        "protected_objects": len(baseline),
                    },
                )
        except Exception as exc:
            self.storage.unpublish(rev_dir)
            self.storage.unpublish(baseline_path)
            raise PublicationError(f"Could not publish the imported revision: {exc}") from exc
        self.queue.put(render_job_id)

    # -- edit ---------------------------------------------------------------

    async def _run_edit(self, job: dict[str, Any]) -> None:
        pid, job_id, base = job["project_id"], job["id"], job["base_revision_id"]
        params = job["params"]
        baseline = json.loads(self.storage.baseline_path(pid).read_text(encoding="utf-8"))
        protected = set(baseline)
        work = self.storage.new_staging(job_id)
        out_dir = work / "revision"
        out_dir.mkdir()
        candidate = out_dir / "model.blend"
        edited = await self._blender(
            "apply_edit.py",
            {
                "base": str(self.storage.revision_model(pid, base)),
                "source": params["source"],
                "output": str(candidate),
                "protected_ids": sorted(baseline),
            },
            job_id,
            "edit",
            self.settings.deadlines.edit,
        )
        checked = await self._blender(
            "validate_candidate.py",
            {"candidate": str(candidate), "protected_ids": sorted(baseline)},
            job_id,
            "validate",
            self.settings.deadlines.validate,
        )
        self._require_self_contained(checked)
        report = compare(baseline, checked["fingerprints"])
        script_output = edited.get("output", "")
        if not report["ok"]:
            raise SculptError(
                _count(len(report["violations"]), "protected object")
                + " changed, so the edit was rejected and no revision was saved. Each violation lists hints "
                "for getting the same result without touching the original.",
                code="protected_geometry_modified",
                details={"violations": report["violations"], "script_output": script_output},
            )
        changes = diff_revisions(
            await self._fingerprints(pid, base, baseline),
            checked["fingerprints"],
            protected,
            self._inspection_ids(pid, base),
        )
        checks = self._relevant_checks(checked, changes)
        self._enforce_checks(checks, strict=bool(params.get("strict_checks")), script_output=script_output)
        report = {**report, "changes": changes, "checks": checks}
        write_json_atomic(out_dir / "inspection.json", checked["inspection"])
        write_json_atomic(out_dir / "fingerprints.json", checked["fingerprints"])
        outcome = {
            "protection_report": report,
            "changes": changes,
            "checks": checks,
            "checks_error": checked.get("checks_error"),
            "created_object_ids": edited.get("created_ids", []),
            "script_output": script_output,
        }
        if params.get("dry_run"):
            await self._finish_dry_run(job, work, candidate, protected, changes, outcome)
            return
        revision_id = new_id("rev")
        rev_dir = self.storage.revision_dir(pid, revision_id)
        render_job_id = params["render_job_id"]
        self.storage.publish_dir(out_dir, rev_dir)
        try:
            async with self.db.transaction() as conn:
                number = await self.repo.next_revision_number(conn, pid)
                await self.repo.insert_revision(
                    conn,
                    {
                        "id": revision_id,
                        "project_id": pid,
                        "number": number,
                        "parent_id": base,
                        "summary": params["summary"],
                        "created_at": utcnow(),
                        "blender_version": self.runner.version,
                        "protection_report": report,
                        "job_id": job_id,
                        "author": params.get("author", "agent"),
                    },
                )
                if not await self.repo.move_current_revision(conn, pid, base, revision_id):
                    raise StaleRevision(
                        "The project's current revision changed while the edit ran.",
                        details={"base_revision_id": base},
                    )
                await self.repo.update_job(conn, render_job_id, state="queued", revision_id=revision_id)
                await self.repo.update_job(
                    conn,
                    job_id,
                    state="succeeded",
                    revision_id=revision_id,
                    finished_at=utcnow(),
                    result={
                        "revision_id": revision_id,
                        "revision_number": number,
                        "render_job_id": render_job_id,
                        **outcome,
                    },
                )
        except StaleRevision:
            self.storage.unpublish(rev_dir)
            raise
        except Exception as exc:
            self.storage.unpublish(rev_dir)
            raise PublicationError(f"Could not publish the edited revision: {exc}") from exc
        self.queue.put(render_job_id)

    async def _finish_dry_run(
        self,
        job: dict[str, Any],
        work: Path,
        candidate: Path,
        protected: set[str],
        changes: dict[str, Any],
        outcome: dict[str, Any],
    ) -> None:
        """Render the unpublished candidate and report; no revision is created."""
        pid, job_id = job["project_id"], job["id"]
        out_dir = work / "renders"
        out_dir.mkdir()
        render_params = job["params"]["render"]
        result: dict[str, Any] = {"dry_run": True, "would_publish": True, "base_revision_id": job["base_revision_id"]}
        final: Path | None = None
        try:
            rendered = await self._render_files(job_id, candidate, out_dir, render_params, protected, changes)
        except SculptError as exc:
            result["render_error"] = exc.to_dict()
            rendered = None
        else:
            final = self.storage.render_dir(pid, job_id)
            self.storage.publish_dir(out_dir, final)
        try:
            async with self.db.transaction() as conn:
                if rendered is not None and final is not None:
                    result["images"] = await self._insert_render_artifacts(
                        conn, pid, None, job_id, final, rendered, int(render_params["resolution"])
                    )
                await self.repo.update_job(
                    conn, job_id, state="succeeded", finished_at=utcnow(), result={**result, **outcome}
                )
        except Exception as exc:
            if final is not None:
                self.storage.unpublish(final)
            raise PublicationError(f"Could not publish the dry-run renders: {exc}") from exc

    async def _fingerprints(self, pid: str, rid: str, baseline: dict[str, Any]) -> dict[str, Any] | None:
        path = self.storage.revision_fingerprints(pid, rid)
        if path.is_file():
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return data
        rev = await self.repo.get_revision(rid)
        return baseline if rev is not None and rev["number"] == 1 else None

    def _inspection_ids(self, pid: str, rid: str) -> set[str]:
        try:
            data = json.loads(self.storage.revision_inspection(pid, rid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {o["id"] for o in data.get("objects", []) if o.get("id")}

    @staticmethod
    def _relevant_checks(checked: dict[str, Any], changes: dict[str, Any]) -> list[dict[str, Any]]:
        """Checks for objects this edit added or modified (all agent objects if unknown)."""
        checks: list[dict[str, Any]] = checked.get("checks") or []
        if changes.get("modified") is None:
            return checks
        touched = set(changed_ids(changes))
        return [c for c in checks if c.get("object_id") in touched]

    @staticmethod
    def _enforce_checks(checks: list[dict[str, Any]], *, strict: bool, script_output: str) -> None:
        blocking = {"error", "warning"} if strict else {"error"}
        failing = [
            {**c, "issues": [i for i in c["issues"] if i.get("severity") in blocking]}
            for c in checks
            if any(i.get("severity") in blocking for i in c["issues"])
        ]
        if failing:
            raise SculptError(
                _count(len(failing), "new or changed object")
                + " failed geometry checks, so the edit was rejected and no revision was saved."
                + ("" if strict else " Errors always reject; warnings reject only with strict_checks."),
                code="geometry_check_failed",
                details={"objects": failing, "script_output": script_output},
            )

    @staticmethod
    def _require_self_contained(checked: dict[str, Any]) -> None:
        issues = checked.get("issues") or []
        if issues:
            raise SculptError(
                f"The result depends on {_count(len(issues), 'external item')}; revisions must be self-contained.",
                code="not_self_contained",
                details={"issues": issues[:200]},
            )

    # -- render -------------------------------------------------------------

    async def _render_files(
        self,
        job_id: str,
        model: Path,
        out_dir: Path,
        params: dict[str, Any],
        protected: set[str],
        changes: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Render into `out_dir` and validate every image; returns Blender's result."""
        changed = changed_ids(changes)
        closeups = params.get("closeups")
        if params.get("auto_closeups") and changed:
            closeups = {"object_ids": changed, "views": list(AUTO_CLOSEUP_VIEWS)}
        passes = ["color", *(params.get("passes") or [])]
        result = await self._blender(
            "render_views.py",
            {
                "model": str(model),
                "output_dir": str(out_dir),
                "views": params["views"],
                "cameras": params["cameras"],
                "frame": params["frame"],
                "engine": params["engine"],
                "resolution": params["resolution"],
                "passes": passes,
                "closeups": closeups,
                "protected_ids": sorted(protected),
                "changed_ids": changed,
            },
            job_id,
            "render",
            self.settings.deadlines.render,
        )
        resolution = int(params["resolution"])
        views = result["views"]
        cameras = len(params["views"]) + len(params["cameras"])
        if closeups and closeups.get("object_ids"):
            cameras += len(closeups.get("views") or AUTO_CLOSEUP_VIEWS)
        expected = cameras * len(passes)
        if len(views) != expected:
            raise SculptError(f"Expected {expected} images, Blender produced {len(views)}", code="render_failed")
        for v in views:
            try:
                validate_png(out_dir / v["file"], resolution, resolution)
            except Exception as exc:
                raise SculptError(f"Render output failed validation: {exc}", code="render_failed") from exc
        write_json_atomic(out_dir / "cameras.json", result)
        return result

    async def _insert_render_artifacts(
        self,
        conn: Any,
        pid: str,
        rid: str | None,
        job_id: str,
        final: Path,
        result: dict[str, Any],
        resolution: int,
    ) -> list[dict[str, Any]]:
        artifacts = []
        for v in result["views"]:
            path = final / v["file"]
            aid = new_id("art")
            meta = {
                "revision_id": rid,
                "view": v["view"],
                "base_view": v.get("base_view", v["view"]),
                "pass": v.get("pass", "color"),
                "camera": v["camera"],
                "frame": result["frame"],
                "engine": result["engine"],
                "image": {"width": resolution, "height": resolution, "origin": "top-left"},
                "bounds": result["bounds"],
            }
            if v.get("depth_range") is not None:
                meta["depth_range"] = v["depth_range"]
            if result.get("focus"):
                meta["focus"] = result["focus"]
            await self.repo.insert_artifact(
                conn,
                {
                    "id": aid,
                    "project_id": pid,
                    "revision_id": rid,
                    "job_id": job_id,
                    "kind": "render_image",
                    "view": v["view"],
                    "rel_path": self.storage.relative(path),
                    "mime": "image/png",
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "metadata": meta,
                },
            )
            artifacts.append({"artifact_id": aid, "view": v["view"]})
        return artifacts

    async def _run_render(self, job: dict[str, Any]) -> None:
        pid, job_id, rid = job["project_id"], job["id"], job["revision_id"]
        params = job["params"]
        work = self.storage.new_staging(job_id)
        out_dir = work / "renders"
        out_dir.mkdir()
        baseline = json.loads(self.storage.baseline_path(pid).read_text(encoding="utf-8"))
        revision = await self.repo.get_revision(rid)
        changes = ((revision or {}).get("protection_report") or {}).get("changes")
        result = await self._render_files(
            job_id, self.storage.revision_model(pid, rid), out_dir, params, set(baseline), changes
        )
        final = self.storage.render_dir(pid, job_id)
        self.storage.publish_dir(out_dir, final)
        try:
            async with self.db.transaction() as conn:
                artifacts = await self._insert_render_artifacts(
                    conn, pid, rid, job_id, final, result, int(params["resolution"])
                )
                await self.repo.update_job(
                    conn,
                    job_id,
                    state="succeeded",
                    finished_at=utcnow(),
                    result={
                        "revision_id": rid,
                        "images": artifacts,
                        "frame": result["frame"],
                        "bounds": result["bounds"],
                        "focus": result.get("focus"),
                        "engine": result["engine"],
                        "passes": result.get("passes", ["color"]),
                    },
                )
        except Exception as exc:
            self.storage.unpublish(final)
            raise PublicationError(f"Could not publish renders: {exc}") from exc

    # -- inspect ------------------------------------------------------------

    async def _run_inspect(self, job: dict[str, Any]) -> None:
        pid, job_id, rid = job["project_id"], job["id"], job["revision_id"]
        self.storage.new_staging(job_id)
        baseline = json.loads(self.storage.baseline_path(pid).read_text(encoding="utf-8"))
        result = await self._blender(
            "spatial_query.py",
            {
                "model": str(self.storage.revision_model(pid, rid)),
                "query": job["params"]["query"],
                "revision_id": rid,
                "protected_ids": sorted(baseline),
            },
            job_id,
            "inspect",
            self.settings.deadlines.inspect,
        )
        payload = {"revision_id": rid, "query": job["params"]["query"], **result}
        payload.pop("ok", None)
        payload.pop("_elapsed_seconds", None)
        if "derived_ray" in job["params"]:
            payload["derived_ray"] = job["params"]["derived_ray"]
        if len(json.dumps(payload)) > MAX_INSPECT_RESULT_BYTES:
            raise SculptError("Query result exceeds the size bound; lower the limit.", code="result_too_large")
        async with self.db.transaction() as conn:
            await self.repo.update_job(conn, job_id, state="succeeded", finished_at=utcnow(), result=payload)

    # -- export -------------------------------------------------------------

    async def _run_export(self, job: dict[str, Any]) -> None:
        pid, job_id, rid = job["project_id"], job["id"], job["revision_id"]
        work = self.storage.new_staging(job_id)
        staged = work / "model.stl"
        units = job["params"]["stl_units"]
        result = await self._blender(
            "export_stl.py",
            {"model": str(self.storage.revision_model(pid, rid)), "output": str(staged), "units": units},
            job_id,
            "export",
            self.settings.deadlines.export,
        )
        expected_size = 84 + 50 * int(result["triangles"])
        if not staged.is_file() or staged.stat().st_size != expected_size:
            raise SculptError("Exported STL failed validation", code="export_failed")
        aid = new_id("art")
        final: Path = self.storage.exports_dir(pid) / f"{aid}.stl"
        self.storage.publish_file(staged, final)
        revision = await self.repo.get_revision(rid)
        meta = {
            "format": "stl",
            "units": units,
            "triangles": result["triangles"],
            "objects": result["objects"],
            "bounds": result["bounds"],
            "revision_id": rid,
            "revision_number": revision["number"] if revision else None,
            "unit_note": "STL is unitless; coordinates are Blender units x scene scale_length (metres), "
            "x1000 for mm. 'scene' writes raw Blender units.",
        }
        try:
            async with self.db.transaction() as conn:
                await self.repo.insert_artifact(
                    conn,
                    {
                        "id": aid,
                        "project_id": pid,
                        "revision_id": rid,
                        "job_id": job_id,
                        "kind": "export_stl",
                        "view": None,
                        "rel_path": self.storage.relative(final),
                        "mime": "model/stl",
                        "size": final.stat().st_size,
                        "sha256": sha256_file(final),
                        "metadata": meta,
                    },
                )
                await self.repo.update_job(
                    conn, job_id, state="succeeded", finished_at=utcnow(), result={"artifact_id": aid, **meta}
                )
        except Exception as exc:
            self.storage.unpublish(final)
            raise PublicationError(f"Could not publish the export: {exc}") from exc
