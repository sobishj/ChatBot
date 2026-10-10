"""Entry point of the ``app`` container: ``python -m app.server``.

Runs two independent HTTP servers in one process:
  * admin UI          on ADMIN_PORT
  * public chat API   on APP_PORT
Keeping them on separate ports lets on-premise installs expose only the public port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

from app.admin_app import create_admin_app
from app.config import get_settings
from app.db.startup import prepare_database
from app.embeddings.model import preload_embedder
from app.llm.client import preload_litellm
from app.logging_setup import configure_logging
from app.public_app import create_public_app

logger = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to :func:`serve`.

    Each uvicorn server normally installs its own SIGINT/SIGTERM handlers; with two
    servers in one process the second would overwrite the first, so only one would stop.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _server(app: FastAPI, port: int, forwarded_allow_ips: str, log_level: str) -> _Server:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips,
        log_config=None,  # keep our logging configuration
        log_level=log_level.lower(),
    )
    return _Server(config)


async def serve() -> None:
    """Start both servers and stop them together on SIGINT/SIGTERM."""
    settings = get_settings()
    configure_logging(settings.log_level)
    prepare_database()
    preload_embedder()  # chat answers need it; loading takes 15-30 s on a CPU
    preload_litellm()  # ~2 s import, otherwise paid by the first chat answer

    servers = [
        _server(create_admin_app(), settings.admin_port, settings.forwarded_allow_ips, settings.log_level),
        _server(create_public_app(), settings.app_port, settings.forwarded_allow_ips, settings.log_level),
    ]

    def stop() -> None:
        logger.info("Shutdown requested")
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # add_signal_handler is unavailable on Windows
            loop.add_signal_handler(sig, stop)

    logger.info("Admin UI on port %s, public API on port %s", settings.admin_port, settings.app_port)
    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(serve())
