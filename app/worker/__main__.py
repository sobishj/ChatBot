"""Entry point of the ``worker`` container: ``python -m app.worker``.

Runs the job queue consumer and the scheduler (automatic re-crawls, folder scans,
data retention). Only one worker container should run the scheduler.
"""

from __future__ import annotations

import logging
import signal
from types import FrameType

from app.config import get_settings
from app.db.startup import prepare_database
from app.logging_setup import configure_logging
from app.worker import handlers  # noqa: F401 - registers job handlers
from app.worker.runner import Worker
from app.worker.scheduler import start_scheduler

logger = logging.getLogger("app.worker")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    prepare_database()

    worker = Worker(threads=2)
    scheduler = start_scheduler()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s, stopping", signum)
        worker.stop()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    worker.run()  # blocks until stopped
    scheduler.shutdown(wait=False)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
