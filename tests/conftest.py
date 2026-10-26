"""Isolated QA adapters; synthetic files never represent real Blender coverage."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sculpt_workspace.app import create_app
from sculpt_workspace.blender.discovery import BlenderStatus
from sculpt_workspace.blender.runner import BlenderJobError, UnavailableRunner
from sculpt_workspace.config import Settings

BASELINE = {"obj_original": {"name": "Original", "type": "MESH", "components": {"coordinates": "original"}}}


class SyntheticRunner:
    """Exercise real orchestration and persistence, without running geometry code."""

    version = "synthetic-qa"

    def __init__(self) -> None:
        self.reject = False
        self.fail_render = False

    async def run(
        self, script: str, request: dict[str, Any], *, workdir: Path, deadline: float, log_path: Path
    ) -> dict[str, Any]:
        if script == "import_project.py":
            Path(request["output_dir"], "model.blend").write_bytes(b"BLENDER-synthetic-revision")
            return {"fingerprints": BASELINE}
        if script == "apply_edit.py":
            self.reject = request["source"] == "reject"
            Path(request["output"]).write_bytes(b"BLENDER-synthetic-edit")
            return {"output": "synthetic output", "created_ids": []}
        if script == "validate_candidate.py":
            fp = BASELINE
            if self.reject:
                fp = {"obj_original": {**BASELINE["obj_original"], "components": {"coordinates": "modified"}}}
            return {"fingerprints": fp, "inspection": {"objects": [{"id": "obj_original"}]}, "issues": []}
        if script == "render_views.py":
            if self.fail_render:
                raise BlenderJobError("Synthetic render failure", code="blender_crashed")
            views = []
            names = request["views"] + [c["name"] for c in request["cameras"]]
            closeups = request.get("closeups") or {}
            if closeups.get("object_ids"):
                names += [f"closeup_{v}" for v in closeups.get("views") or ["front", "perspective"]]
            for name in names:
                for pass_name in request.get("passes") or ["color"]:
                    view = name if pass_name == "color" else f"{name}--{pass_name}"
                    filename = view + ".png"
                    Image.new("RGB", (request["resolution"], request["resolution"]), "gray").save(
                        Path(request["output_dir"], filename)
                    )
                    views.append(
                        {
                            "view": view,
                            "base_view": name,
                            "pass": pass_name,
                            "file": filename,
                            "camera": {"type": "orthographic", "lens_mm": 50},
                        }
                    )
            return {"views": views, "frame": request["frame"], "engine": "BLENDER_WORKBENCH", "bounds": {}}
        if script == "export_stl.py":
            Path(request["output"]).write_bytes(bytes(80) + struct.pack("<I", 1) + bytes(50))
            return {"triangles": 1, "objects": 1, "bounds": {}}
        if script == "spatial_query.py":
            return {"ok": True, "hits": [{"object_id": "obj_original"}]}
        raise AssertionError(script)

    async def shutdown(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = SyntheticRunner()

    async def build(settings: Settings):
        return runner, BlenderStatus(True, None, runner.version, "Synthetic QA adapter")

    monkeypatch.setattr("sculpt_workspace.app.build_runner", build)
    settings = Settings(
        data_dir=tmp_path, port=18766, mcp_token="qa-only-token", max_upload_bytes=1024, standard_resolution=64
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:18766") as browser:
        page = browser.get("/")
        import re

        browser.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        browser.qa_runner = runner
        yield browser


@pytest.fixture
def unavailable_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def build(settings: Settings):
        return UnavailableRunner("QA: absent"), BlenderStatus(False, None, None, "QA: absent")

    monkeypatch.setattr("sculpt_workspace.app.build_runner", build)
    with TestClient(
        create_app(Settings(data_dir=tmp_path, port=18766, mcp_token="qa-only-token")),
        base_url="http://127.0.0.1:18766",
    ) as browser:
        yield browser
