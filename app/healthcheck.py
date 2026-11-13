"""Container healthcheck. Exit 0 = healthy.

Checks the database only — it is the one dependency whose loss makes the app useless.
Deliberately does NOT hit external sites, so a network blip never marks us unhealthy.
"""

from __future__ import annotations

import asyncio
import sys


async def _main() -> int:
    from app.db import dispose_engine, ping

    try:
        ok = await ping()
    finally:
        await dispose_engine()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck error: {exc}", file=sys.stderr)
        sys.exit(1)

