"""Notifier interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Message:
    """A rendered notification ready to send."""

    title: str
    body: str
    type: str = "GENERIC"
    urgency: str = "normal"          # normal | high
    meta: dict[str, Any] = field(default_factory=dict)

    def as_text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()


@dataclass
class SendResult:
    ok: bool
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(self, message: Message) -> SendResult:
        """Deliver one message. Must not raise; return SendResult(ok=False, error=...)."""

