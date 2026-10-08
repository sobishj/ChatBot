"""Container health probe: ``python -m app.healthcheck`` exits 0 when the admin server is healthy.

Used by the Docker healthcheck and install.sh, so the image needs no curl.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from app.config import get_settings


def main() -> int:
    url = f"http://127.0.0.1:{get_settings().admin_port}/health"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:  # fixed local URL
            return 0 if resp.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
