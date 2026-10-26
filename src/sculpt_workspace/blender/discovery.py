"""Locate Blender, validate its version, and run the startup self-check."""

from __future__ import annotations

import asyncio
import glob
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from .runner import RunnerLimits, SubprocessRunner, UnavailableRunner

log = logging.getLogger("sculpt.blender")

TARGET_SERIES = (5, 2)
_VERSION = re.compile(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass
class BlenderStatus:
    available: bool
    path: str | None
    version: str | None
    message: str

    def to_dict(self) -> dict[str, object]:
        return {"available": self.available, "path": self.path, "version": self.version, "message": self.message}


def candidate_paths() -> list[Path]:
    found: list[Path] = []
    if sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            found.append(base / "Blender.app" / "Contents" / "MacOS" / "Blender")
            for app in sorted(base.glob("Blender*.app"), reverse=True):
                found.append(app / "Contents" / "MacOS" / "Blender")
    which = shutil.which("blender")
    if which:
        found.append(Path(which))
    found += [Path("/usr/bin/blender"), Path("/usr/local/bin/blender"), Path("/snap/bin/blender")]
    for pattern in ("/opt/blender*/blender", str(Path.home() / "blender*" / "blender")):
        found += [Path(p) for p in sorted(glob.glob(pattern), reverse=True)]
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def discover(override: str | None) -> Path | None:
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


def parse_version(output: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def probe_version(path: Path, timeout: float = 60.0) -> str:
    out = subprocess.run(
        [str(path), "--factory-startup", "--version"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(), "LANG": "C.UTF-8"},
        check=False,
    )
    return out.stdout + out.stderr


async def build_runner(settings: Settings) -> tuple[SubprocessRunner | UnavailableRunner, BlenderStatus]:
    path = discover(settings.blender_path)
    if path is None:
        where = settings.blender_path or "standard locations"
        msg = (
            f"Blender was not found ({where}). Install Blender 5.2 LTS or set SCULPT_BLENDER_PATH "
            "to the Blender executable."
        )
        return UnavailableRunner(msg), BlenderStatus(False, settings.blender_path, None, msg)
    try:
        raw = await asyncio.to_thread(probe_version, path)
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"Blender at {path} could not be started: {exc}"
        return UnavailableRunner(msg), BlenderStatus(False, str(path), None, msg)
    parsed = parse_version(raw)
    if parsed is None:
        msg = f"Could not read the Blender version from {path}"
        return UnavailableRunner(msg), BlenderStatus(False, str(path), None, msg)
    version = ".".join(str(p) for p in parsed)
    if parsed[:2] != TARGET_SERIES and not settings.allow_any_blender_version:
        msg = (
            f"Blender {version} found at {path}; Sculpt Workspace targets Blender "
            f"{TARGET_SERIES[0]}.{TARGET_SERIES[1]} LTS. Set SCULPT_ALLOW_ANY_BLENDER_VERSION=1 to override."
        )
        return UnavailableRunner(msg), BlenderStatus(False, str(path), version, msg)
    runner = SubprocessRunner(path, version, RunnerLimits(settings.max_log_bytes, settings.blender_env_passthrough))
    if settings.skip_blender_startup_check:
        return runner, BlenderStatus(True, str(path), version, "Startup render check skipped")
    ok, detail = await startup_check(runner, settings)
    if not ok:
        msg = f"Blender {version} failed the startup Workbench render: {detail}"
        return UnavailableRunner(msg), BlenderStatus(False, str(path), version, msg)
    return runner, BlenderStatus(True, str(path), version, "Ready")


async def startup_check(runner: SubprocessRunner, settings: Settings) -> tuple[bool, str]:
    from ..errors import SculptError

    work = settings.staging_dir / "startup-check"
    work.mkdir(parents=True, exist_ok=True)
    try:
        result = await runner.run(
            "startup_check.py",
            {"output": str(work / "check.png")},
            workdir=work,
            deadline=settings.deadlines.startup_check,
            log_path=settings.logs_dir / "startup-check.log",
        )
    except SculptError as exc:
        return False, exc.message
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return True, str(result.get("engine", "workbench"))
