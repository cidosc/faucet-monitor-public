"""Telegram notifier with an optional user-configured proxy.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.notify.base import Message, SendResult

log = get_logger(__name__)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 proxy: str | None = None) -> None:
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.proxy = proxy if proxy is not None else (settings.telegram_proxy or None)

    @property
    def configured(self) -> bool:
        return bool(settings.telegram_enabled and self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"{settings.telegram_api_base}/bot{self.token}/{method}"

    async def send(self, message: Message) -> SendResult:
        if not self.configured:
            return SendResult(False, "telegram_not_configured_or_disabled")

        payload = {
            "chat_id": self.chat_id,
            "text": message.as_text()[:4096],
            "disable_web_page_preview": True,
        }
        kwargs: dict[str, object] = {"timeout": 20.0}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            async with httpx.AsyncClient(**kwargs) as client:  # type: ignore[arg-type]
                resp = await client.post(self._url("sendMessage"), json=payload)
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code != 200 or not body.get("ok", False):
                return SendResult(
                    False,
                    f"telegram_http_{resp.status_code}: {str(body.get('description'))[:200]}",
                    {"body": body},
                )
            return SendResult(True, None, {"message_id": body.get("result", {}).get("message_id")})
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram_send_failed", extra={"error": str(exc), "proxy": bool(self.proxy)})
            return SendResult(False, f"{type(exc).__name__}: {exc}")

