"""Application configuration, read from environment variables.

Every setting has a local-first default so `python -m sculpt_workspace` works
without any configuration. Variables are prefixed with ``SCULPT_``.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SculptWorkspace"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "sculpt-workspace"


@dataclass(frozen=True)
class Deadlines:
    """Per-job-kind Blender execution deadlines in seconds."""

    startup_check: float = 120.0
    import_: float = 900.0
    edit: float = 600.0
    validate: float = 900.0
    render: float = 900.0
    inspect: float = 300.0
    export: float = 600.0


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8765
    blender_path: str | None = None
    mcp_token: str | None = None
    max_upload_bytes: int = 512 * MIB
    queue_capacity: int = 32
    max_log_bytes: int = 1 * MIB
    standard_resolution: int = 1024
    deadlines: Deadlines = field(default_factory=Deadlines)
    allow_any_blender_version: bool = False
    skip_blender_startup_check: bool = False
    # Environment variables passed through to Blender in addition to the
    # minimal set. Display variables let GPU renderers find a context.
    blender_env_passthrough: tuple[str, ...] = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sculpt.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "mcp_token"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        names = {"127.0.0.1", "localhost", "[::1]"}
        return frozenset(f"{n}:{self.port}" for n in names)

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{h}" for h in self.allowed_hosts)

    def validate(self) -> None:
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback:
            raise ValueError(f"SCULPT_HOST must be a loopback address, got {self.host!r}")
        if not 1 <= self.port <= 65535:
            raise ValueError("SCULPT_PORT must be between 1 and 65535")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def load_settings() -> Settings:
    data_dir = os.environ.get("SCULPT_DATA_DIR")
    deadlines = Deadlines(
        startup_check=_env_float("SCULPT_DEADLINE_STARTUP", Deadlines.startup_check),
        import_=_env_float("SCULPT_DEADLINE_IMPORT", Deadlines.import_),
        edit=_env_float("SCULPT_DEADLINE_EDIT", Deadlines.edit),
        validate=_env_float("SCULPT_DEADLINE_VALIDATE", Deadlines.validate),
        render=_env_float("SCULPT_DEADLINE_RENDER", Deadlines.render),
        inspect=_env_float("SCULPT_DEADLINE_INSPECT", Deadlines.inspect),
        export=_env_float("SCULPT_DEADLINE_EXPORT", Deadlines.export),
    )
    settings = Settings(
        data_dir=Path(data_dir).expanduser() if data_dir else default_data_dir(),
        host=os.environ.get("SCULPT_HOST", "127.0.0.1"),
        port=int(os.environ.get("SCULPT_PORT", "8765")),
        blender_path=os.environ.get("SCULPT_BLENDER_PATH") or None,
        mcp_token=os.environ.get("SCULPT_MCP_TOKEN") or None,
        max_upload_bytes=int(float(os.environ.get("SCULPT_MAX_UPLOAD_MB", "512")) * MIB),
        deadlines=deadlines,
        allow_any_blender_version=_env_bool("SCULPT_ALLOW_ANY_BLENDER_VERSION", False),
        skip_blender_startup_check=_env_bool("SCULPT_SKIP_BLENDER_CHECK", False),
    )
    settings.validate()
    return settings


def ensure_mcp_token(settings: Settings) -> str:
    """Return the MCP bearer token, generating a private token file if needed.

    The token is never logged; callers may print the file path only.
    """
    if settings.mcp_token:
        return settings.mcp_token
    path = settings.token_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            os.chmod(path, 0o600)
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token
