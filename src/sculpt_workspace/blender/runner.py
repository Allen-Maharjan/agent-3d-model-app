"""Isolated Blender subprocess execution.

Each invocation gets factory settings, auto-exec disabled, an argument array
(never a shell), a private working directory and a minimal environment. The
process runs in its own process group so the whole tree can be terminated on
timeout or shutdown. This is operational isolation, not a security sandbox:
authenticated agent scripts are trusted local code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..errors import BlenderUnavailable, SculptError

log = logging.getLogger("sculpt.blender")

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "blender_scripts"
TERMINATE_GRACE_SECONDS = 5.0


class BlenderJobError(SculptError):
    status = 500
    code = "blender_failed"


class Runner(Protocol):
    @property
    def version(self) -> str: ...

    async def run(
        self, script: str, request: dict[str, Any], *, workdir: Path, deadline: float, log_path: Path
    ) -> dict[str, Any]: ...

    async def shutdown(self) -> None: ...


@dataclass
class RunnerLimits:
    max_log_bytes: int
    env_passthrough: tuple[str, ...] = ()


def minimal_env(workdir: Path, executable: Path, passthrough: tuple[str, ...]) -> dict[str, str]:
    """Build the subprocess environment from scratch: nothing is inherited
    except an explicit allow-list, so tokens and credentials never leak in."""
    home = workdir / "home"
    tmp = workdir / "tmp"
    user = workdir / "blender-user"
    for d in (home, tmp, user):
        d.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join([str(executable.parent), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "BLENDER_USER_RESOURCES": str(user),
        "BLENDER_USER_CONFIG": str(user / "config"),
        "BLENDER_USER_SCRIPTS": str(user / "scripts"),
        "BLENDER_USER_EXTENSIONS": str(user / "extensions"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in passthrough:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def build_command(executable: Path, script: Path, request_path: Path, result_path: Path) -> list[str]:
    return [
        str(executable),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "3",
        "--python",
        str(script),
        "--",
        str(request_path),
        str(result_path),
    ]


class SubprocessRunner:
    """Runs one Blender script per call. The job worker guarantees that only
    one call is in flight at a time."""

    def __init__(self, executable: Path, version: str, limits: RunnerLimits):
        self.executable = executable
        self._version = version
        self.limits = limits
        self._active: asyncio.subprocess.Process | None = None

    @property
    def version(self) -> str:
        return self._version

    async def run(
        self, script: str, request: dict[str, Any], *, workdir: Path, deadline: float, log_path: Path
    ) -> dict[str, Any]:
        script_path = SCRIPTS_DIR / script
        if not script_path.is_file():
            raise BlenderJobError(f"Unknown Blender script {script}")
        request_path = workdir / "request.json"
        result_path = workdir / "result.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        result_path.unlink(missing_ok=True)
        cmd = build_command(self.executable, script_path, request_path, result_path)
        env = minimal_env(workdir, self.executable, self.limits.env_passthrough)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._active = proc
        timed_out = False
        try:
            pump = asyncio.create_task(self._pump_log(proc, log_path))
            try:
                await asyncio.wait_for(proc.wait(), timeout=deadline)
            except TimeoutError:
                timed_out = True
                await self._terminate(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(pump, timeout=TERMINATE_GRACE_SECONDS)
        finally:
            self._active = None
        elapsed = time.monotonic() - started
        if timed_out:
            raise BlenderJobError(
                f"Blender exceeded the {deadline:.0f}s deadline and was stopped",
                code="blender_timeout",
                details={"deadline_seconds": deadline, "elapsed_seconds": round(elapsed, 2)},
            )
        result = self._read_result(result_path)
        if result is None:
            raise BlenderJobError(
                "Blender exited without producing a result",
                code="blender_crashed",
                details={"exit_code": proc.returncode, "log_tail": _tail(log_path)},
            )
        result.setdefault("_elapsed_seconds", round(elapsed, 3))
        if not result.get("ok"):
            err = result.get("error") or {}
            raise BlenderJobError(
                str(err.get("message") or "Blender script failed"),
                code=str(err.get("code") or "blender_script_failed"),
                details=dict(err.get("details") or {}),
            )
        return result

    async def _pump_log(self, proc: asyncio.subprocess.Process, log_path: Path) -> None:
        assert proc.stdout is not None
        written = 0
        limit = self.limits.max_log_bytes
        truncated = False
        with log_path.open("wb") as fh:
            os.chmod(log_path, 0o600)
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if written < limit:
                    piece = chunk[: limit - written]
                    fh.write(piece)
                    written += len(piece)
                elif not truncated:
                    fh.write(b"\n[log truncated]\n")
                    truncated = True

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def shutdown(self) -> None:
        proc = self._active
        if proc is not None and proc.returncode is None:
            await self._terminate(proc)

    @staticmethod
    def _read_result(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


class UnavailableRunner:
    """Stands in when Blender is missing so the app still serves the UI and
    reports a clear, actionable error for Blender-backed jobs."""

    def __init__(self, reason: str):
        self.reason = reason

    @property
    def version(self) -> str:
        return "unavailable"

    async def run(
        self, script: str, request: dict[str, Any], *, workdir: Path, deadline: float, log_path: Path
    ) -> dict[str, Any]:
        raise BlenderUnavailable(self.reason)

    async def shutdown(self) -> None:
        return None


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")
