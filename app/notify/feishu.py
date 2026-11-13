"""Feishu (Lark) custom-bot webhook notifier.

Signature scheme (official):
    string_to_sign = f"{timestamp}\\n{secret}"
    sign = base64(hmac_sha256(key=string_to_sign, msg=b""))
and `timestamp` + `sign` go in the JSON body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.notify.base import Message, SendResult

log = get_logger(__name__)


def gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuNotifier:
    name = "feishu"

    def __init__(self, webhook_url: str | None = None, secret: str | None = None) -> None:
        self.webhook_url = webhook_url or settings.feishu_webhook_url
        self.secret = secret if secret is not None else settings.feishu_secret

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, message: Message) -> SendResult:
        if not self.configured:
            return SendResult(False, "feishu_webhook_url_not_configured")

        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": message.as_text()[:4000]},
        }
        if self.secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = gen_sign(ts, self.secret)

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    self.webhook_url,
                    content=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
            body: dict[str, object] = {}
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {"raw": resp.text[:300]}

            # Feishu returns HTTP 200 even on logical failure; check the body.
            code = body.get("code", body.get("StatusCode"))
            if resp.status_code != 200:
                return SendResult(False, f"http_{resp.status_code}", {"body": body})
            if code not in (0, None):
                return SendResult(False, f"feishu_code_{code}", {"body": body})
            return SendResult(True, None, {"body": body})

        except Exception as exc:  # noqa: BLE001
            log.warning("feishu_send_failed", extra={"error": str(exc)})
            return SendResult(False, f"{type(exc).__name__}: {exc}")

