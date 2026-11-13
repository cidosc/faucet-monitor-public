"""Structured airdrop listing source.

Collects dated, paginated listings from configured WordPress REST endpoints.
Availability and schema are provider-dependent; listings are untrusted candidates.

WHAT IT PRODUCES
    One `ProjectCandidate` per listing: the project name, its date, and a link to the
    aggregator's detail page (where the official claim link lives). Plus a
    `task_title`, so the discover job files a MANUAL_REQUIRED task — the point is to
    put it in front of a human, not to act.

WHAT IT DOES NOT DO
    * It does not scrape the detail pages to hunt for claim contracts. Their links are
      JS-rendered and the extraction would be brittle guesswork.
    * It does not set any trust flag. `automation_allowed` stays UNKNOWN, and every
      lead carries a note saying it is unverified aggregator content.
    * It is not the only source, per the requirement not to depend on a single
      third-party airdrop site.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.sources.airdrops import guess_chain, slugify
from app.sources.base import ProjectCandidate

log = get_logger(__name__)

DEFAULT_LOOKBACK_DAYS = 14


def _clean_title(raw: str) -> str:
    import html
    import re

    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw or ""))).strip()


class AirdropListingSource:
    name = "airdrop_api"

    def __init__(self, endpoints: list[dict[str, Any]] | None = None) -> None:
        self.endpoints = endpoints or []

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> AirdropListingSource:
        eps = [
            e for e in (cfg.get("airdrop_api") or [])
            if e.get("enabled", True) and e.get("endpoint")
        ]
        return cls(eps)

    async def discover(self) -> list[ProjectCandidate]:
        if not self.endpoints:
            return []

        out: list[ProjectCandidate] = []
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.http_user_agent, "Accept": "application/json"},
        ) as client:
            for ep in self.endpoints:
                try:
                    out.extend(await self._one(client, ep))
                except Exception as exc:  # noqa: BLE001 - one endpoint must not break others
                    log.warning(
                        "airdrop_listing_failed",
                        extra={"endpoint": ep.get("endpoint"), "error": str(exc)},
                    )
        log.info(
            "airdrop_listing_complete",
            extra={"endpoints": len(self.endpoints), "candidates": len(out)},
        )
        return out

    async def _one(self, client: httpx.AsyncClient, ep: dict[str, Any]) -> list[ProjectCandidate]:
        endpoint = ep["endpoint"]
        per_page = int(ep.get("per_page", 30))
        label = ep.get("name", endpoint)

        params: dict[str, Any] = {
            "per_page": per_page,
            "orderby": "date",
            "order": "desc",
            "_fields": "id,date,modified,slug,link,title,status",
        }

        # Limit requests to recent items instead of re-reading the full archive.
        if ep.get("lookback_days"):
            since = datetime.now(UTC) - timedelta(days=int(ep["lookback_days"]))
            params["after"] = since.strftime("%Y-%m-%dT%H:%M:%S")

        resp = await client.get(endpoint, params=params)
        if resp.status_code != 200:
            log.info(
                "airdrop_listing_http_error",
                extra={"endpoint": endpoint, "status": resp.status_code},
            )
            return []
        try:
            items = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("airdrop_listing_bad_json", extra={"endpoint": endpoint, "error": str(exc)})
            return []
        if not isinstance(items, list):
            log.info("airdrop_listing_unexpected_shape", extra={"endpoint": endpoint})
            return []

        total = resp.headers.get("x-wp-total")
        candidates: list[ProjectCandidate] = []
        for item in items:
            title = _clean_title((item.get("title") or {}).get("rendered", ""))
            slug = item.get("slug") or ""
            link = item.get("link") or ""
            if not title or not link:
                continue
            if item.get("status") not in (None, "publish"):
                continue

            chain_key = guess_chain(title)
            candidates.append(
                ProjectCandidate(
                    # Deterministic slug => re-running is idempotent, no duplicates.
                    slug=slugify(f"{label}-{slug or title}"),
                    name=title[:200],
                    source_plugin=self.name,
                    source_url=link,
                    chain_key=chain_key,
                    docs_url=link,
                    faucet_urls=[],  # aggregator page is not a faucet; do not probe it
                    notes=(
                        f"Unverified listing from {label} ({item.get('date', '')[:10]}). "
                        "Open the detail page to find the project's own claim link, then "
                        "confirm the announcement on the project's own channels. Do not "
                        "assume claiming is permitted to be automated."
                    ),
                    raw={
                        "listing_source": label,
                        "detail_url": link,
                        "listed_at": item.get("date"),
                        "modified": item.get("modified"),
                        "chain_guess": chain_key,
                        "automation_allowed": "UNKNOWN",
                    },
                    task_title=f"Review airdrop listing: {title[:120]}",
                )
            )

        log.info(
            "airdrop_listing_parsed",
            extra={
                "endpoint": endpoint,
                "source": label,
                "returned": len(items),
                "kept": len(candidates),
                "total_available": total,
            },
        )
        return candidates

