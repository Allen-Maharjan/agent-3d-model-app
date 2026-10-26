"""Thin HTTP adapter over the application services."""

from __future__ import annotations

import html
import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .errors import InvalidRequest, PayloadTooLarge, SculptError
from .models import BriefRequest, CheckoutRequest, EditRequest, ExportRequest, InspectRequest, RenderRequest
from .security import SESSION_COOKIE, SessionStore, cookie_value, session_cookie_header
from .services import MAX_REFERENCE_BYTES, Workspace

WEB_DIR = Path(__file__).resolve().parent / "web"
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _ws(request: Request) -> Workspace:
    return request.app.state.workspace


def _error_response(exc: SculptError) -> JSONResponse:
    return JSONResponse({"error": exc.to_dict()}, status_code=exc.status)


def _validation_details(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    return [{"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg")} for e in exc.errors()]


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise InvalidRequest("Request body must be JSON") from exc
    if not isinstance(body, dict):
        raise InvalidRequest("Request body must be a JSON object")
    return body


def _model(cls: Any, data: dict[str, Any]) -> Any:
    try:
        return cls.model_validate(data)
    except ValidationError as exc:
        raise InvalidRequest("Invalid request", details={"errors": _validation_details(exc)}) from exc


def download_name(base: str, suffix: str) -> str:
    cleaned = _FILENAME_SAFE.sub("-", base).strip("-.") or "model"
    return f"{cleaned[:80]}{suffix}"


def install(app: FastAPI, sessions: SessionStore) -> None:
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.exception_handler(SculptError)
    async def _sculpt_error(_: Request, exc: SculptError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(InvalidRequest("Invalid request", details={"errors": _validation_details(exc)}))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request, project_id: str | None = None) -> HTMLResponse:
        sid = cookie_value({k.lower(): v for k, v in request.headers.items()}, SESSION_COOKIE)
        csrf = sessions.csrf_for(sid)
        headers = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if csrf is None:
            sid, csrf = sessions.create()
            headers["Set-Cookie"] = session_cookie_header(sid)
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{CSRF_TOKEN}}", html.escape(csrf))
        return HTMLResponse(page, headers=headers)

    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ws = _ws(request)
        return {
            "blender": ws.blender_status.to_dict(),
            "queue": {
                "outstanding": ws.queue.outstanding,
                "capacity": ws.queue.capacity,
                "running": ws.queue.current_job,
            },
            "limits": {"max_upload_bytes": ws.settings.max_upload_bytes},
        }

    @router.get("/projects")
    async def list_projects(request: Request) -> dict[str, Any]:
        return {"projects": await _ws(request).list_projects()}

    @router.post("/projects", status_code=202)
    async def upload(request: Request, filename: str) -> dict[str, Any]:
        ws = _ws(request)
        limit = ws.settings.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise PayloadTooLarge(
                f"The file is larger than the {limit // (1024 * 1024)} MiB upload limit.",
                details={"limit_bytes": limit, "size_bytes": int(declared)},
            )
        stage_name = f"upload-{uuid.uuid4().hex}"
        stage = ws.storage.new_staging(stage_name)
        target = stage / "upload.blend"
        written = 0
        try:
            with target.open("wb") as fh:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise PayloadTooLarge(
                            f"The file is larger than the {limit // (1024 * 1024)} MiB upload limit.",
                            details={"limit_bytes": limit},
                        )
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            return await ws.create_project(target, filename)
        finally:
            ws.storage.discard_staging(stage_name)

    @router.get("/projects/{project_id}")
    async def project(request: Request, project_id: str) -> dict[str, Any]:
        return await _ws(request).project_detail(project_id)

    @router.get("/projects/{project_id}/jobs")
    async def job_history(
        request: Request,
        project_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        return await _ws(request).job_history(project_id, offset, limit)

    @router.get("/projects/{project_id}/revisions/{revision_id}/inspection")
    async def inspection(request: Request, project_id: str, revision_id: str) -> dict[str, Any]:
        req = _model(InspectRequest, {"project_id": project_id, "revision_id": revision_id})
        return await _ws(request).inspect(req)

    @router.post("/projects/{project_id}/inspect", status_code=202)
    async def inspect(request: Request, project_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return await _ws(request).inspect(_model(InspectRequest, {**body, "project_id": project_id}))

    @router.post("/projects/{project_id}/edits", status_code=202)
    async def submit_edit(request: Request, project_id: str) -> dict[str, Any]:
        """Edits from the browser's code editor; validated exactly like agent edits."""
        body = await _json_body(request)
        return await _ws(request).submit_edit(_model(EditRequest, {**body, "project_id": project_id}), author="user")

    @router.get("/projects/{project_id}/revisions/{revision_id}/source")
    async def revision_source(request: Request, project_id: str, revision_id: str) -> dict[str, Any]:
        return await _ws(request).revision_source(project_id, revision_id)

    @router.post("/projects/{project_id}/checkout")
    async def checkout(request: Request, project_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return await _ws(request).checkout(_model(CheckoutRequest, {**body, "project_id": project_id}))

    @router.post("/projects/{project_id}/renders", status_code=202)
    async def render(request: Request, project_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return await _ws(request).render(_model(RenderRequest, {**body, "project_id": project_id}))

    @router.post("/projects/{project_id}/exports", status_code=202)
    async def export(request: Request, project_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return await _ws(request).export(_model(ExportRequest, {**body, "project_id": project_id}))

    @router.get("/projects/{project_id}/revisions/{revision_id}/model.blend")
    async def download_revision(request: Request, project_id: str, revision_id: str) -> FileResponse:
        path, info = await _ws(request).revision_file(project_id, revision_id)
        name = download_name(f"{info['project']['name']}-r{info['revision']['number']}", ".blend")
        return FileResponse(path, media_type="application/x-blender", filename=name)

    @router.put("/projects/{project_id}/brief")
    async def brief(request: Request, project_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return await _ws(request).set_brief(_model(BriefRequest, {**body, "project_id": project_id}))

    @router.post("/projects/{project_id}/references", status_code=201)
    async def add_reference(request: Request, project_id: str, filename: str = "", note: str = "") -> dict[str, Any]:
        too_large = PayloadTooLarge(
            f"Reference images are limited to {MAX_REFERENCE_BYTES // (1024 * 1024)} MiB.",
            details={"limit_bytes": MAX_REFERENCE_BYTES},
        )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_REFERENCE_BYTES:
            raise too_large
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_REFERENCE_BYTES:
                raise too_large
        if not data:
            raise InvalidRequest("The uploaded image is empty.", code="invalid_image")
        return await _ws(request).add_reference(project_id, bytes(data), filename, note)

    @router.get("/projects/{project_id}/references/{reference_id}")
    async def reference(request: Request, project_id: str, reference_id: str) -> FileResponse:
        path = await _ws(request).reference_file(project_id, reference_id)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=31536000"})

    @router.delete("/projects/{project_id}/references/{reference_id}")
    async def delete_reference(request: Request, project_id: str, reference_id: str) -> dict[str, Any]:
        return await _ws(request).delete_reference(project_id, reference_id)

    @router.get("/jobs/{job_id}")
    async def job(request: Request, job_id: str) -> dict[str, Any]:
        return await _ws(request).job(job_id)

    @router.get("/artifacts/{artifact_id}")
    async def artifact(request: Request, artifact_id: str, download: bool = False) -> FileResponse:
        path, art = await _ws(request).artifact_file(artifact_id)
        if art["kind"] == "render_image" and not download:
            return FileResponse(
                path, media_type=art["mime"], headers={"Cache-Control": "private, max-age=31536000, immutable"}
            )
        meta = art["metadata"] or {}
        suffix = ".stl" if art["kind"] == "export_stl" else Path(path).suffix
        label = (
            f"r{meta.get('revision_number')}-{meta.get('units', '')}"
            if art["kind"] == "export_stl"
            else art["view"] or "image"
        )
        return FileResponse(path, media_type=art["mime"], filename=download_name(f"sculpt-{label}", suffix))

    app.include_router(router)
