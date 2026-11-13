"""RSS/Atom source: official project blogs.

Finds posts that mention testnet/faucet/airdrop and proposes them as candidates with a
link back to the post. It does NOT claim the post announces a faucet — it flags it for
review, which is the honest thing a text heuristic can do.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from app.http import Fetcher
from app.logging_conf import get_logger
from app.sources.base import ProjectCandidate

log = get_logger(__name__)

KEYWORDS = re.compile(
    r"\b(testnet|faucet|airdrop|devnet|incentivized|points program|quest)\b", re.IGNORECASE
)
FAUCET_LINK = re.compile(r"https?://[^\s\"'<>]*(faucet|gimme|drip)[^\s\"'<>]*", re.IGNORECASE)
NS = {"atom": "http://www.w3.org/2005/Atom"}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:80] or "unnamed"


class RssSource:
    name = "rss"

    def __init__(self, feeds: list[dict[str, Any]] | None = None) -> None:
        self.feeds = feeds or []

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> RssSource:
        feeds = [f for f in (cfg.get("rss") or []) if f.get("enabled", True)]
        return cls(feeds)

    async def discover(self) -> list[ProjectCandidate]:
        if not self.feeds:
            return []
        out: list[ProjectCandidate] = []
        async with Fetcher() as fetcher:
            for feed in self.feeds:
                url = feed.get("url")
                if not url:
                    continue
                try:
                    out.extend(await self._one(fetcher, feed))
                except Exception as exc:  # noqa: BLE001
                    log.warning("rss_feed_failed", extra={"url": url, "error": str(exc)})
        return out

    async def _one(self, fetcher: Fetcher, feed: dict[str, Any]) -> list[ProjectCandidate]:
        url = feed["url"]
        res = await fetcher.get(url, check_robots=False)  # feeds are meant to be machine-read
        if not res.ok or not res.text:
            log.info("rss_fetch_failed", extra={"url": url, "status": res.status, "error": res.error})
            return []

        try:
            root = ET.fromstring(res.text)
        except ET.ParseError as exc:
            log.warning("rss_parse_failed", extra={"url": url, "error": str(exc)})
            return []

        items = root.findall(".//item") or root.findall(".//atom:entry", NS)
        found: list[ProjectCandidate] = []
        for item in items[:30]:
            title = (item.findtext("title") or item.findtext("atom:title", default="", namespaces=NS)).strip()
            link = (item.findtext("link") or "").strip()
            if not link:
                link_el = item.find("atom:link", NS)
                link = (link_el.get("href") if link_el is not None else "") or ""
            desc = (
                item.findtext("description")
                or item.findtext("atom:summary", default="", namespaces=NS)
                or ""
            )
            blob = f"{title} {desc}"
            if not KEYWORDS.search(blob):
                continue
            faucet_links = FAUCET_LINK.findall(blob)
            found.append(
                ProjectCandidate(
                    slug=_slugify(f"{feed.get('name','feed')}-{title}"),
                    name=title[:200] or "(untitled post)",
                    source_plugin=self.name,
                    source_url=link or url,
                    chain_key=feed.get("default_chain"),
                    docs_url=link or url,
                    faucet_urls=faucet_links,
                    notes=f"RSS match from {feed.get('name', url)}. Not verified — review manually.",
                    raw={"feed": url, "title": title},
                )
            )
        return found

