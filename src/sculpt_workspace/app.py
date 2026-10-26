"""Application assembly: FastAPI + MCP behind the security gateway."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from .api import install
from .blender.discovery import build_runner
from .config import Settings, ensure_mcp_token
from .db import Database
from .mcp_tools import build_mcp
from .security import Gateway, SessionStore
from .services import Workspace
from .storage import Storage

log = logging.getLogger("sculpt")


def create_app(settings: Settings) -> ASGIApp:
    settings.validate()
    storage = Storage(settings)
    storage.init()
    token = ensure_mcp_token(settings)
    sessions = SessionStore()
    holder: dict[str, Workspace] = {}

    def get_ws() -> Workspace:
        return holder["ws"]

    base_url = f"http://127.0.0.1:{settings.port}"
    mcp_server = build_mcp(get_ws, base_url)
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sorted(settings.allowed_hosts),
            allowed_origins=sorted(settings.allowed_origins),
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.db_path)
        await db.open()
        runner, status = await build_runner(settings)
        if status.available:
            log.info("Blender %s ready at %s", status.version, status.path)
        else:
            log.warning("Blender unavailable: %s", status.message)
        ws = Workspace(settings, db, storage, runner, status)
        holder["ws"] = ws
        app.state.workspace = ws
        await ws.start()
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            await ws.stop()
            await db.close()

    api = FastAPI(title="Sculpt Workspace", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    install(api, sessions)
    return Gateway(api, mcp_app, settings, token, sessions)
