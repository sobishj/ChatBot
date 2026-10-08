"""Entry point of the ``worker`` container: ``python -m app.worker``.

Step 1 placeholder: it starts, verifies the database connection periodically and
shuts down cleanly. The job queue (step 7) and scheduler (step 16) are added later.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from app.config import get_settings
from app.db.session import check_database
from app.logging_setup import configure_logging

logger = logging.getLogger("app.worker")

POLL_SECONDS = 30


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s, stopping", signum)
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    logger.info("Worker started")
    while not stop.is_set():
        status = check_database()
        if not status["ok"]:
            logger.warning("Database not reachable (%s)", status.get("error"))
        stop.wait(POLL_SECONDS)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
