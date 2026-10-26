"""Parameterised SQLite queries.

Read methods acquire the database lock themselves. Write methods take the
connection yielded by ``Database.transaction()`` so callers compose several
writes into one explicit transaction.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .db import Database, utcnow

JSON_COLUMNS = {"view_frame", "error", "protection_report", "params", "result", "metadata"}
ACTIVE_STATES = ("blocked", "queued", "running")
MUTATION_KINDS = ("import", "edit")


_LATEST_RENDER = """
    SELECT * FROM jobs WHERE revision_id = ? AND kind = 'render' AND json_extract(params, '$.standard') = 1
    ORDER BY created_at DESC, rowid DESC LIMIT 1
"""
_LATEST_SUCCEEDED_RENDER = """
    SELECT * FROM jobs WHERE revision_id = ? AND kind = 'render' AND json_extract(params, '$.standard') = 1
      AND state = 'succeeded'
    ORDER BY created_at DESC, rowid DESC LIMIT 1
"""


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_COLUMNS and value is not None:
            value = json.loads(value)
        out[key] = value
    return out


def _rows(rows: list[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [r for r in (_row(x) for x in rows) if r is not None]


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"), sort_keys=True)


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # -- projects -----------------------------------------------------------

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        return _row(await self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,)))

    @staticmethod
    async def get_project_conn(conn: aiosqlite.Connection, project_id: str) -> dict[str, Any]:
        async with conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cur:
            row = _row(await cur.fetchone())
        if row is None:
            raise LookupError(project_id)
        return row

    async def list_projects(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT p.*, r.number AS current_revision_number,
                   (SELECT COUNT(*) FROM revisions x WHERE x.project_id = p.id) AS revision_count
            FROM projects p LEFT JOIN revisions r ON r.id = p.current_revision_id
            ORDER BY p.updated_at DESC LIMIT ?
            """,
            (limit,),
        )
        return _rows(rows)

    @staticmethod
    async def insert_project(conn: aiosqlite.Connection, project: dict[str, Any]) -> None:
        now = utcnow()
        await conn.execute(
            """
            INSERT INTO projects (id, name, original_filename, original_size, original_sha256, status,
                                  current_revision_id, view_frame, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'importing', NULL, NULL, NULL, ?, ?)
            """,
            (
                project["id"],
                project["name"],
                project["original_filename"],
                project["original_size"],
                project["original_sha256"],
                now,
                now,
            ),
        )

    @staticmethod
    async def set_project_status(
        conn: aiosqlite.Connection, project_id: str, status: str, error: dict[str, Any] | None = None
    ) -> None:
        await conn.execute(
            "UPDATE projects SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, _json(error), utcnow(), project_id),
        )

    @staticmethod
    async def move_current_revision(
        conn: aiosqlite.Connection, project_id: str, expected: str | None, new: str
    ) -> bool:
        """Compare-and-set the current revision pointer. Returns False if stale."""
        cur = await conn.execute(
            """
            UPDATE projects SET current_revision_id = ?, updated_at = ?
            WHERE id = ? AND current_revision_id IS ?
            """,
            (new, utcnow(), project_id, expected),
        )
        return cur.rowcount == 1

    @staticmethod
    async def set_brief(conn: aiosqlite.Connection, project_id: str, brief: str | None) -> None:
        await conn.execute("UPDATE projects SET brief = ?, updated_at = ? WHERE id = ?", (brief, utcnow(), project_id))

    @staticmethod
    async def set_view_frame(conn: aiosqlite.Connection, project_id: str, frame: dict[str, Any]) -> None:
        await conn.execute(
            "UPDATE projects SET view_frame = ?, updated_at = ? WHERE id = ?", (_json(frame), utcnow(), project_id)
        )

    # -- revisions ----------------------------------------------------------

    async def get_revision(self, revision_id: str) -> dict[str, Any] | None:
        return _row(await self.db.fetchone("SELECT * FROM revisions WHERE id = ?", (revision_id,)))

    async def list_revisions(self, project_id: str) -> list[dict[str, Any]]:
        return _rows(
            await self.db.fetchall("SELECT * FROM revisions WHERE project_id = ? ORDER BY number DESC", (project_id,))
        )

    @staticmethod
    async def next_revision_number(conn: aiosqlite.Connection, project_id: str) -> int:
        async with conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM revisions WHERE project_id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 1

    @staticmethod
    async def insert_revision(conn: aiosqlite.Connection, rev: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO revisions (id, project_id, number, parent_id, summary, created_at,
                                   blender_version, protection_report, job_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rev["id"],
                rev["project_id"],
                rev["number"],
                rev["parent_id"],
                rev["summary"],
                rev["created_at"],
                rev["blender_version"],
                _json(rev["protection_report"]),
                rev["job_id"],
            ),
        )

    # -- jobs ---------------------------------------------------------------

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return _row(await self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,)))

    async def list_jobs(self, project_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """The newest `limit` jobs plus every active job, however old, newest first."""
        marks = ",".join("?" * len(ACTIVE_STATES))
        return _rows(
            await self.db.fetchall(
                f"""
                SELECT * FROM jobs WHERE project_id = ? AND (
                    state IN ({marks})
                    OR id IN (SELECT id FROM jobs WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?)
                )
                ORDER BY created_at DESC, rowid DESC
                """,  # noqa: S608 - placeholders only
                (project_id, *ACTIVE_STATES, project_id, limit),
            )
        )

    async def job_history(self, project_id: str, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        rows = await self.db.fetchall(
            "SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
        total = await self.db.fetchone("SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,))
        return _rows(rows), int(total[0]) if total else 0

    @staticmethod
    async def pending_mutations(conn: aiosqlite.Connection, project_id: str) -> list[str]:
        marks = ",".join("?" * len(ACTIVE_STATES))
        kinds = ",".join("?" * len(MUTATION_KINDS))
        async with conn.execute(
            f"SELECT id FROM jobs WHERE project_id = ? AND state IN ({marks}) AND kind IN ({kinds}) "  # noqa: S608 - placeholders only
            "AND COALESCE(json_extract(params, '$.dry_run'), 0) = 0",
            (project_id, *ACTIVE_STATES, *MUTATION_KINDS),
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    @staticmethod
    async def insert_job(conn: aiosqlite.Connection, job: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO jobs (id, project_id, kind, state, params, result, error, base_revision_id,
                              revision_id, parent_job_id, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["project_id"],
                job["kind"],
                job["state"],
                _json(job.get("params", {})),
                job.get("base_revision_id"),
                job.get("revision_id"),
                job.get("parent_job_id"),
                utcnow(),
            ),
        )

    @staticmethod
    async def update_job(conn: aiosqlite.Connection, job_id: str, **fields: Any) -> None:
        allowed = {"state", "result", "error", "revision_id", "started_at", "finished_at", "params"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown job fields: {unknown}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        values = [_json(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        await conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*values, job_id))  # noqa: S608 - column names are allow-listed

    @staticmethod
    async def fail_interrupted_jobs(conn: aiosqlite.Connection) -> list[str]:
        async with conn.execute("SELECT id FROM jobs WHERE state IN ('blocked', 'queued', 'running')") as cur:
            ids = [r[0] for r in await cur.fetchall()]
        error = _json(
            {
                "code": "interrupted",
                "message": "The server stopped before this job finished. Submit it again.",
                "details": {},
            }
        )
        await conn.execute(
            "UPDATE jobs SET state = 'failed', error = ?, finished_at = ? "
            "WHERE state IN ('blocked','queued','running')",
            (error, utcnow()),
        )
        await conn.execute(
            """
            UPDATE projects SET status = 'import_failed', error = ?, updated_at = ?
            WHERE status = 'importing' AND current_revision_id IS NULL
            """,
            (error, utcnow()),
        )
        return ids

    # -- artifacts ----------------------------------------------------------

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return _row(await self.db.fetchone("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)))

    async def job_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        return _rows(
            await self.db.fetchall("SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at, rowid", (job_id,))
        )

    async def latest_standard_render(self, revision_id: str, succeeded_only: bool = False) -> dict[str, Any] | None:
        """Most recent render job for a revision that covers the standard views."""
        sql = _LATEST_SUCCEEDED_RENDER if succeeded_only else _LATEST_RENDER
        return _row(await self.db.fetchone(sql, (revision_id,)))

    async def all_artifact_paths(self) -> set[str]:
        return {r[0] for r in await self.db.fetchall("SELECT rel_path FROM artifacts")}

    async def all_revision_ids(self) -> set[str]:
        return {r[0] for r in await self.db.fetchall("SELECT id FROM revisions")}

    async def all_project_ids(self) -> set[str]:
        return {r[0] for r in await self.db.fetchall("SELECT id FROM projects")}

    @staticmethod
    async def insert_artifact(conn: aiosqlite.Connection, art: dict[str, Any]) -> None:
        await conn.execute(
            """
            INSERT INTO artifacts (id, project_id, revision_id, job_id, kind, view, rel_path, mime, size,
                                   sha256, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                art["id"],
                art["project_id"],
                art.get("revision_id"),
                art.get("job_id"),
                art["kind"],
                art.get("view"),
                art["rel_path"],
                art["mime"],
                art["size"],
                art["sha256"],
                _json(art.get("metadata", {})),
                utcnow(),
            ),
        )
