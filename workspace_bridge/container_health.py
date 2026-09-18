"""Readiness of both internal listeners, not tunnel/authorization/model health."""
from __future__ import annotations
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def check() -> bool:
    # Ignore proxy environment variables; only exact internal loopback URLs.
    opener = build_opener(ProxyHandler({}))
    for url, expected in (('http://127.0.0.1:8766/', 200), ('http://127.0.0.1:8765/mcp', 401)):
        try:
            with opener.open(url, timeout=2) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
        except OSError:
            return False
        if status != expected:
            return False
    return True


if __name__ == '__main__':
    sys.exit(0 if check() else 1)
