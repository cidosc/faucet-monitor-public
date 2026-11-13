"""Structured logging. JSON by default so ``docker logs`` stays greppable and parseable."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from app.config import settings

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


class _SafeAdapter(logging.LoggerAdapter):
    """Renames `extra` keys that collide with reserved LogRecord attributes.

    `logging` raises KeyError("Attempt to overwrite %r in LogRecord") if `extra` contains
    a reserved key such as `name`, `module`, `msg` or `args`. That turns an innocuous
    log call into a crash — which is exactly how a background job dies mid-run. Rather
    than rely on every call site remembering the reserved list, collisions are renamed
    with a `ctx_` prefix here.
    """

    def process(self, msg, kwargs):  # type: ignore[no-untyped-def]
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"ctx_{k}" if k in _RESERVED else k): v for k, v in extra.items()
            }
        return msg, kwargs


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())

    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # third-party noise control
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")
    logging.getLogger("apscheduler.executors.default").setLevel("WARNING")
    logging.getLogger("apscheduler.scheduler").setLevel("WARNING")


def get_logger(name: str) -> logging.LoggerAdapter:
    """Return a logger that tolerates reserved-key collisions in `extra`."""
    return _SafeAdapter(logging.getLogger(name), {})

