"""Behavioral QA against real HTTP, SQLite, queue, storage, and MCP transport."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sculpt_workspace.config import Settings
from sculpt_workspace.models import RenderRequest, ViewFrame
from sculpt_workspace.protection import compare


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] not in {"queued", "blocked", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Job {job_id} did not finish")


def upload(client: TestClient, name: str = "Sculpture.blend") -> dict:
    response = client.post("/api/projects", params={"filename": name}, content=b"BLENDER-synthetic-input")
    assert response.status_code == 202, response.text
    result = response.json()
    assert wait_job(client, result["import_job_id"])["state"] == "succeeded"
    assert wait_job(client, result["render_job_id"])["state"] == "succeeded"
    return client.get(f"/api/projects/{result['project_id']}").json()


@pytest.mark.parametrize("path", ["/", "/projects/unknown", "/static/app.js", "/static/app.css", "/static/icons.svg"])
def test_public_pages_and_assets(client, path):
    assert client.get(path).status_code == 200


def test_session_csp_and_csrf(client):
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert "{{CSRF_TOKEN}}" not in response.text
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/projects?filename=x.blend", content=b"BLENDER").status_code == 403
    client.headers["X-CSRF-Token"] = token
    client.cookies.clear()
    assert client.get("/api/projects").status_code == 401


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Host": "attacker.example:18766"}, 421),
        ({"Origin": "https://attacker.example"}, 403),
        ({"Origin": "null"}, 403),
    ],
)
def test_host_origin_rejection(client, headers, status):
    assert client.get("/api/health", headers=headers).status_code == status


@pytest.mark.parametrize(
    "filename,content,code",
    [
        ("x.txt", b"BLENDER", "invalid_file_type"),
        ("x.blend", b"", "invalid_blend_file"),
        ("x.blend", b"garbage", "invalid_blend_file"),
        ("x.blend", b"BLENDER" + bytes(1024), "upload_too_large"),
    ],
)
def test_bad_upload(client, filename, content, code):
    response = client.post("/api/projects", params={"filename": filename}, content=content)
    assert response.status_code in {413, 422}
    assert response.json()["error"]["code"] == code
    assert client.get("/api/projects").json() == {"projects": []}


def test_unavailable_engine_still_serves_browser(unavailable_client):
    client = unavailable_client
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json()["blender"]["available"] is False


def test_import_render_inspection_download_and_original_bytes(client):
    data = upload(client)
    pid = data["project"]["project_id"]
    rid = data["current_revision"]["revision_id"]
    assert data["project"]["status"] == "ready"
    assert data["current_revision"]["protection"]["ok"] is True
    assert len(data["renders"]["images"]) == 4
    for image in data["renders"]["images"]:
        response = client.get(image["url"])
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
    assert client.get(f"/api/projects/{pid}/revisions/{rid}/model.blend").content == b"BLENDER-synthetic-revision"
    inspection = client.get(f"/api/projects/{pid}/revisions/{rid}/inspection").json()
    assert inspection["inspection"]["objects"][0]["protected"] is True
    ws = client.app.api.state.workspace
    assert ws.storage.original_path(pid).read_bytes() == b"BLENDER-synthetic-input"
    assert ws.storage.original_path(pid).stat().st_mode & 0o222 == 0
    assert list(ws.storage.staging.iterdir()) == []


def edit(client, data, source="pass"):
    from sculpt_workspace.models import EditRequest

    ws = client.app.api.state.workspace
    return client.portal.call(
        ws.submit_edit,
        EditRequest(
            project_id=data["project"]["project_id"],
            base_revision_id=data["current_revision"]["revision_id"],
            source=source,
            summary="QA edit",
        ),
    )


def test_edit_and_checkout_preserve_history(client):
    data = upload(client)
    pid = data["project"]["project_id"]
    first = data["current_revision"]["revision_id"]
    jobs = edit(client, data)
    assert wait_job(client, jobs["edit_job_id"])["state"] == "succeeded"
    wait_job(client, jobs["render_job_id"])
    data = client.get(f"/api/projects/{pid}").json()
    second = data["current_revision"]["revision_id"]
    assert data["current_revision"]["number"] == 2
    assert data["current_revision"]["parent_id"] == first
    response = client.post(
        f"/api/projects/{pid}/checkout", json={"revision_id": first, "expected_current_revision_id": second}
    )
    assert response.status_code == 200
    assert len(client.get(f"/api/projects/{pid}").json()["revisions"]) == 2
    stale = client.post(
        f"/api/projects/{pid}/checkout", json={"revision_id": second, "expected_current_revision_id": second}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"


def test_rejected_edit_keeps_current_and_cancels_child(client):
    data = upload(client)
    result = edit(client, data, "reject")
    job = wait_job(client, result["edit_job_id"])
    assert job["state"] == "failed"
    assert job["error"]["code"] == "protected_geometry_modified"
    assert wait_job(client, result["render_job_id"])["state"] == "cancelled"
    after = client.get(f"/api/projects/{data['project']['project_id']}").json()
    assert after["current_revision"] == data["current_revision"]
    assert len(after["revisions"]) == 1


def test_failed_render_retains_previous_images_and_download(client):
    data = upload(client)
    pid, rid = data["project"]["project_id"], data["current_revision"]["revision_id"]
    client.qa_runner.fail_render = True
    response = client.post(f"/api/projects/{pid}/renders", json={"revision_id": rid, "resolution": 64})
    assert response.status_code == 202
    assert wait_job(client, response.json()["job_id"])["state"] == "failed"
    after = client.get(f"/api/projects/{pid}").json()
    assert after["renders"]["images"] == data["renders"]["images"]
    assert client.get(f"/api/projects/{pid}/revisions/{rid}/model.blend").status_code == 200


@pytest.mark.parametrize("units", ["mm", "m", "scene"])
def test_export_job_artifact_and_units(client, units):
    data = upload(client)
    pid, rid = data["project"]["project_id"], data["current_revision"]["revision_id"]
    response = client.post(
        f"/api/projects/{pid}/exports", json={"revision_id": rid, "format": "stl", "stl_units": units}
    )
    job = wait_job(client, response.json()["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["units"] == units
    artifact = client.get(f"/api/artifacts/{job['result']['artifact_id']}?download=1")
    assert len(artifact.content) == 134
    assert "attachment" in artifact.headers["content-disposition"]


def test_cross_project_revision_rejected(client):
    a, b = upload(client), upload(client, "Other.blend")
    response = client.get(
        f"/api/projects/{a['project']['project_id']}/revisions/{b['current_revision']['revision_id']}/model.blend"
    )
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/api/projects/missing", "/api/jobs/missing", "/api/artifacts/missing"])
def test_not_found(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("body", [[], "text", {"unexpected": True}, {"resolution": 0}, {"views": ["invalid"]}])
def test_render_invalid_body(client, body):
    assert client.post("/api/projects/missing/renders", json=body).status_code == 422


def test_mcp_auth_and_tool_list(client):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert client.post("/mcp", json=payload).status_code == 401
    response = client.post(
        "/mcp",
        json=payload,
        headers={"Authorization": "Bearer qa-only-token", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200, response.text
    assert {tool["name"] for tool in response.json()["result"]["tools"]} == {
        "get_project",
        "get_playbook",
        "inspect_project",
        "apply_edit",
        "render_views",
        "get_result",
        "checkout_revision",
        "export_model",
    }


def test_protection_missing_changed_and_new_objects():
    base = {"a": {"components": {"coordinates": "x"}}}
    assert compare(base, base)["ok"] is True
    assert compare(base, {})["ok"] is False
    assert compare(base, {"a": {"components": {"coordinates": "y"}}})["ok"] is False
    assert compare(base, {**base, "b": {"name": "New"}})["agent_objects"] == [{"object_id": "b", "name": "New"}]


@pytest.mark.parametrize("frame", [{"front": [0, 0, 0]}, {"front": [0, 0, 1]}, {"front": [float("nan"), 0, 0]}])
def test_invalid_frame_rejected(frame):
    with pytest.raises(ValidationError):
        ViewFrame.model_validate(frame)


def test_non_loopback_binding_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, host="0.0.0.0").validate()


def test_duplicate_camera_names_rejected():
    with pytest.raises(ValidationError):
        RenderRequest(project_id="p", views=["front", "front"])


def test_queue_full_rejects_upload_without_leaking_files(client):
    ws = client.app.api.state.workspace
    ws.queue.reserve(ws.queue.capacity)
    try:
        response = client.post("/api/projects?filename=full.blend", content=b"BLENDER-qa")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "queue_full"
        assert client.get("/api/projects").json()["projects"] == []
        assert not list(ws.storage.staging.iterdir())
        assert not list(ws.storage.projects.iterdir())
    finally:
        ws.queue.release(ws.queue.capacity)


def test_publication_failure_rolls_back_database_and_files(client, monkeypatch):
    import sqlite3

    ws = client.app.api.state.workspace

    async def fail_insert(*args, **kwargs):
        raise sqlite3.IntegrityError("Synthetic commit failure")

    monkeypatch.setattr(ws.repo, "insert_revision", fail_insert)
    response = client.post("/api/projects?filename=rollback.blend", content=b"BLENDER-qa")
    result = response.json()
    job = wait_job(client, result["import_job_id"])
    assert job["state"] == "failed"
    assert job["error"]["code"] == "publication_failed"
    assert wait_job(client, result["render_job_id"])["state"] == "cancelled"
    data = client.get(f"/api/projects/{result['project_id']}").json()
    assert data["current_revision"] is None
    assert data["revisions"] == []
    assert not ws.storage.baseline_path(result["project_id"]).exists()
    assert not list((ws.storage.project_dir(result["project_id"]) / "revisions").glob("*"))


def test_restart_marks_interrupted_jobs_and_keeps_committed_revision(client):
    data = upload(client)
    ws = client.app.api.state.workspace
    pid = data["project"]["project_id"]

    async def restart():
        await ws.stop()
        async with ws.db.transaction() as conn:
            await ws.repo.insert_job(
                conn, {"id": "interrupted_qa", "project_id": pid, "kind": "render", "state": "running"}
            )
        ws.storage.new_staging("orphan_qa")
        await ws.start()

    client.portal.call(restart)
    job = client.get("/api/jobs/interrupted_qa").json()
    assert job["state"] == "failed"
    assert job["error"]["code"] == "interrupted"
    assert not list(ws.storage.staging.iterdir())
    assert client.get(f"/api/projects/{pid}").json()["current_revision"] == data["current_revision"]


def test_mcp_get_result_includes_actual_png_blocks(client):
    data = upload(client)
    job_id = data["renders"]["shown_job_id"]
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_result", "arguments": {"job_id": job_id}},
        },
        headers={"Authorization": "Bearer qa-only-token", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result.get("isError"), result
    assert len([block for block in result["content"] if block["type"] == "image"]) == 4


def test_spatial_query_job_result_contract(client):
    data = upload(client)
    pid = data["project"]["project_id"]
    response = client.post(
        f"/api/projects/{pid}/inspect",
        json={"revision_id": data["current_revision"]["revision_id"], "query": {"type": "nearest", "point": [0, 0, 0]}},
    )
    assert response.status_code == 202
    job = wait_job(client, response.json()["job_id"])
    assert job["result"]["hits"][0]["object_id"] == "obj_original"


def test_active_job_survives_history_limit(client):
    """An old running job stays in project detail behind 30 newer queued and 30 newer finished jobs."""
    data = upload(client)
    ws = client.app.api.state.workspace
    pid = data["project"]["project_id"]

    async def seed():
        async with ws.db.transaction() as conn:
            await ws.repo.insert_job(
                conn, {"id": "old_active", "project_id": pid, "kind": "inspect", "state": "running"}
            )
            for number in range(30):
                await ws.repo.insert_job(
                    conn, {"id": f"new_queued_{number}", "project_id": pid, "kind": "inspect", "state": "queued"}
                )
            for number in range(30):
                await ws.repo.insert_job(
                    conn, {"id": f"new_done_{number}", "project_id": pid, "kind": "inspect", "state": "succeeded"}
                )

    client.portal.call(seed)
    assert client.get("/api/jobs/old_active").json()["state"] == "running"
    detail = client.get(f"/api/projects/{pid}").json()
    ids = {job["job_id"] for job in detail["jobs"]}
    assert "old_active" in ids
    assert all(f"new_queued_{n}" in ids for n in range(30))
    assert detail["jobs_total"] == 63  # import + render + 61 seeded
    page = client.get(f"/api/projects/{pid}/jobs", params={"offset": 60, "limit": 50}).json()
    assert page["total"] == 63
    assert len(page["jobs"]) == 3


def test_brief_save_trim_clear_and_cached_inspection(client):
    data = upload(client)
    pid, rid = data["project"]["project_id"], data["current_revision"]["revision_id"]
    response = client.put(f"/api/projects/{pid}/brief", json={"brief": "  Add a flower  "})
    assert response.status_code == 200
    assert response.json()["brief"] == "Add a flower"
    assert client.get(f"/api/projects/{pid}").json()["project"]["brief"] == "Add a flower"
    assert client.get(f"/api/projects/{pid}/revisions/{rid}/inspection").json()["brief"] == "Add a flower"
    assert client.put(f"/api/projects/{pid}/brief", json={"brief": ""}).json()["brief"] == ""


@pytest.mark.parametrize("payload", [{"brief": "x" * 4001}, {"brief": "ok", "unknown": True}, {}])
def test_invalid_brief_rejected(client, payload):
    assert client.put("/api/projects/missing/brief", json=payload).status_code == 422


def reference_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def test_reference_upload_download_scope_delete(client):
    a, b = upload(client), upload(client, "Other.blend")
    pid = a["project"]["project_id"]
    response = client.post(
        f"/api/projects/{pid}/references",
        params={"filename": "../test.png", "note": " petal "},
        content=reference_png(),
    )
    assert response.status_code == 201
    ref = response.json()
    assert ref["filename"] == "test.png"
    assert ref["note"] == "petal"
    assert client.get(ref["url"]).content.startswith(b"\x89PNG")
    assert len(client.get(f"/api/projects/{pid}").json()["project"]["references"]) == 1
    other = b["project"]["project_id"]
    assert client.get(f"/api/projects/{other}/references/{ref['reference_id']}").status_code == 404
    assert client.delete(f"/api/projects/{other}/references/{ref['reference_id']}").status_code == 404
    assert client.delete(ref["url"]).status_code == 200
    assert client.get(ref["url"]).status_code == 404


@pytest.mark.parametrize("content", [b"", b"not an image"])
def test_reference_invalid_content(client, content):
    data = upload(client)
    pid = data["project"]["project_id"]
    response = client.post(f"/api/projects/{pid}/references", content=content)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_image"


def test_reference_count_bounded(client):
    data = upload(client)
    pid = data["project"]["project_id"]
    for _ in range(8):
        assert client.post(f"/api/projects/{pid}/references", content=reference_png()).status_code == 201
    assert client.post(f"/api/projects/{pid}/references", content=reference_png()).status_code == 409


def test_dry_run_preserves_current_and_returns_preview_artifacts(client):
    from sculpt_workspace.models import EditRequest

    data = upload(client)
    ws = client.app.api.state.workspace
    pid = data["project"]["project_id"]
    result = client.portal.call(
        ws.submit_edit,
        EditRequest(
            project_id=pid,
            base_revision_id=data["current_revision"]["revision_id"],
            source="pass",
            summary="QA preview",
            dry_run=True,
        ),
    )
    job = wait_job(client, result["edit_job_id"])
    assert job["state"] == "succeeded", job
    assert job["result"]["dry_run"] is True
    assert {artifact["view"] for artifact in job["artifacts"]} == {
        view + suffix for view in ("front", "right", "back", "perspective") for suffix in ("", "--highlight")
    }
    after = client.get(f"/api/projects/{pid}").json()
    assert after["current_revision"] == data["current_revision"]
    assert len(after["revisions"]) == 1
