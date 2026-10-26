"""Entry point: ``python -m sculpt_workspace``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import load_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    app = create_app(settings)
    log = logging.getLogger("sculpt")
    log.info("Data directory: %s", settings.data_dir)
    if not settings.mcp_token:
        log.info("MCP bearer token file: %s", settings.token_path)
    log.info("Browser: http://localhost:%d  MCP: http://127.0.0.1:%d/mcp", settings.port, settings.port)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        workers=1,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
