"""Optional disposable server for browser QA; no actual Blender execution."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import sculpt_workspace.app as application
from conftest import SyntheticRunner
from sculpt_workspace.blender.discovery import BlenderStatus
from sculpt_workspace.config import Settings


async def synthetic_runner(settings: Settings):
    runner = SyntheticRunner()
    return runner, BlenderStatus(True, None, runner.version, "Synthetic QA only; Blender is not installed")


if __name__ == "__main__":
    application.build_runner = synthetic_runner
    with TemporaryDirectory(prefix="sculpt-qa-browser-") as data_dir:
        uvicorn.run(
            application.create_app(Settings(data_dir=Path(data_dir), port=18766, standard_resolution=64)),
            host="127.0.0.1",
            port=18766,
        )
