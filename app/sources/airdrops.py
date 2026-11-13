"""Airdrop feed source: discovery from public announcement feeds.

WHAT THIS IS
    A poller for a *configurable list* of airdrop-announcement feeds (aggregator RSS plus
    mainstream crypto news). It turns new entries into `ProjectCandidate`s for human
    review.

WHAT THIS IS NOT
    * It does not verify that an airdrop is real, live, or safe. Everything it produces is
      marked `source_plugin="airdrops"` with `automation_allowed=UNKNOWN` and no trust
      flags set. A candidate here is a *lead*, not a fact.
    * It is not the only source. The requirements explicitly say not to depend on a single
      third-party airdrop site, so this runs alongside `manual`, `rss` and `github`, and
      the curated `manual` file always wins on conflicts.
    * It never follows "claim now" links automatically and never interacts with a claim
      page. It records URLs for a human to open.

Feeds are fetched through the shared HTTP layer (timeouts, retries, per-host rate limit,
custom UA). Feed endpoints are machine-readable APIs, so robots.txt is not applied to
them, but the rate limit is.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any

from app.http import Fetcher
from app.logging_conf import get_logger
from app.sources.base import ProjectCandidate

log = get_logger(__name__)

ATOM = {"atom": "http://www.w3.org/2005/Atom"}

# Terms that make an entry worth looking at.
INTEREST = re.compile(
    r"\b(airdrop|claim|token launch|tge|points program|season \d|snapshot|"
    r"whitelist|allowlist|retroactive|eligib)\w*\b",
    re.IGNORECASE,
)

# An entry only deserves attention if it reads like a LIVE ANNOUNCEMENT. Merely containing
# the word "airdrop" is not enough — that is how you end up collecting listicles such as
# "The 15 biggest airdrops ever" and irrelevance like "Trezor says hackers breached its
# email provider". Requiring either a claim URL or announcement phrasing is what turns
# this from a keyword matcher into a signal.
ANNOUNCE = re.compile(
    r"("
    r"\bairdrop\b[^.]{0,40}\b(is |are |now |goes |went )?(live|open|claimable|active)\b"
    r"|\bclaim\b[^.]{0,30}\b(is |now |goes )?(live|open|available|enabled)\b"
    r"|\b(now|officially|finally)\s+(live|claimable|open)\b"
    r"|\bannounc(e|es|ed|ing)\b[^.]{0,60}\b(airdrop|token launch|tge|distribution)\b"
    r"|\bclaim your\b"
    r"|\bairdrop (checker|eligibility|portal|claim)\b"
    r"|\beligible (addresses|wallets|users) can claim\b"
    r"|\bsnapshot (taken|complete[d]?)\b"
    r"|\btoken (launch|generation event|is launching)\b"
    r"|\bclaim (window|period) (opens|is open|now open)\b"
    r")",
    re.IGNORECASE,
)

# Editorial formats. These are written ABOUT airdrops; they are not airdrops. They were the
# entire yield of the first version of this filter, which is why the pattern is explicit.
EDITORIAL = re.compile(
    r"("
    r"^\s*(top\s+\d+|best\s|\d+\s+(biggest|best|top|passive|proven))"
    r"|\bstrateg(y|ies)\b|\bplaybook\b|\bguide\b|\bexplained\b|\bwhat (is|are)\b"
    r"|\bhow to\b|\btutorial\b|\bfor beginners\b|\bworth\b|\bever\b"
    r"|\brecap\b|\broundup\b|\bweekly\b|\bthis week\b|\bnewsletter\b"
    r"|\bprice (prediction|analysis|forecast|target)\b|\bhow to buy\b"
    r"|\bhack(ed|ers|ing)?\b|\bbankrupt(cy)?\b|\blawsuit\b|\bsec (sues|charges)\b"
    r"|\bmeme coin (explained|targeting)\b|\bstimulus\b"
    r"|\bpassive (income|airdrop)\b|\bfarming (strategy|playbook)\b"
    r"|\bscam\b|\bphishing\b|\brug ?pull\b|\balready distributed\b"
    r")",
    re.IGNORECASE,
)

# Chain hints, mapped to our chain keys where one exists.
CHAIN_HINTS: list[tuple[re.Pattern[str], str | None]] = [
    (re.compile(r"\b(ethereum|eth mainnet|\bETH\b)", re.I), "ethereum"),
    (re.compile(r"\bbase\b", re.I), "base"),
    (re.compile(r"\barbitrum\b", re.I), "arbitrum"),
    (re.compile(r"\boptimism\b|\bOP Mainnet\b", re.I), "optimism"),
    (re.compile(r"\bpolygon\b|\bmatic\b|\bPOL\b", re.I), "polygon"),
    (re.compile(r"\bBNB Chain\b|\bBSC\b|\bbinance smart chain\b", re.I), "bnb"),
    # Recognised but not implemented in v1 — recorded so the candidate is still useful.
    (re.compile(r"\bsolana\b|\bSOL\b", re.I), None),
    (re.compile(r"\btron\b|\bTRX\b", re.I), None),
    (re.compile(r"\bbitcoin\b|\bordinals\b|\brunes\b", re.I), None),
]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")

# Links that look like the actual claim/airdrop destination rather than an ad or share link.
CLAIMISH = re.compile(r"(claim|airdrop|rewards?|quest|eligib|distribut|drop)", re.I)
JUNK_HOSTS = re.compile(
    r"(twitter\.com|x\.com|facebook\.com|t\.me|discord|linkedin|reddit|"
    r"feedburner|google\.com|doubleclick|bit\.ly|utm_)",
    re.I,
)


def _text(raw: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()


def slugify(text: str, limit: int = 70) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:limit].rstrip("-")) or "unnamed"


def _entry_title(item: ET.Element) -> str:
    return _text(item.findtext("title") or item.findtext("atom:title", default="", namespaces=ATOM))


def _entry_link(item: ET.Element) -> str:
    link = (item.findtext("link") or "").strip()
    if link:
        return link
    el = item.find("atom:link", ATOM)
    if el is not None:
        return (el.get("href") or "").strip()
    return ""


def _entry_body(item: ET.Element) -> str:
    raw = (
        item.findtext("description")
        or item.findtext("atom:summary", default="", namespaces=ATOM)
        or item.findtext("content")
        or item.findtext("atom:content", default="", namespaces=ATOM)
        or ""
    )
    return _text(raw)


def guess_chain(text: str) -> str | None:
    for pattern, key in CHAIN_HINTS:
        if pattern.search(text):
            return key
    return None


def extract_claim_urls(body: str, entry_link: str) -> list[str]:
    """Pick out plausible claim/airdrop destinations, dropping social + tracking links."""
    out: list[str] = []
    for url in _URL.findall(body or ""):
        url = url.rstrip(".,;:")
        if JUNK_HOSTS.search(url):
            continue
        if not CLAIMISH.search(url):
            continue
        if url not in out:
            out.append(url)
        if len(out) >= 5:
            break
    return out


class AirdropFeedSource:
    name = "airdrops"

    def __init__(self, feeds: list[dict[str, Any]] | None = None) -> None:
        self.feeds = feeds or []

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> AirdropFeedSource:
        feeds = [f for f in (cfg.get("airdrops") or []) if f.get("enabled", True) and f.get("url")]
        return cls(feeds)

    async def discover(self) -> list[ProjectCandidate]:
        if not self.feeds:
            return []

        found: list[ProjectCandidate] = []
        async with Fetcher() as fetcher:
            for feed in self.feeds:
                try:
                    found.extend(await self._one(fetcher, feed))
                except Exception as exc:  # noqa: BLE001 - one dead feed must not stop the rest
                    log.warning(
                        "airdrop_feed_failed",
                        extra={"feed": feed.get("name"), "url": feed.get("url"),
                               "error": str(exc)},
                    )
        log.info("airdrop_source_complete", extra={"feeds": len(self.feeds), "candidates": len(found)})
        return found

    async def _one(self, fetcher: Fetcher, feed: dict[str, Any]) -> list[ProjectCandidate]:
        url = feed["url"]
        # Feed endpoints are APIs meant for machine consumption; robots.txt is not applied
        # to them, but the per-host rate limit still is.
        res = await fetcher.get(url, check_robots=False)
        if not res.ok or not res.text:
            log.info(
                "airdrop_feed_unavailable",
                extra={"url": url, "status": res.status, "error": res.error},
            )
            return []

        try:
            root = ET.fromstring(res.text)
        except ET.ParseError as exc:
            log.warning("airdrop_feed_parse_failed", extra={"url": url, "error": str(exc)})
            return []

        items = root.findall(".//item") or root.findall(".//atom:entry", ATOM)
        candidates: list[ProjectCandidate] = []
        stats = {"entries": len(items), "not_interesting": 0, "editorial": 0,
                 "no_signal": 0, "kept": 0}
        for item in items[:40]:
            title = _entry_title(item)
            body = _entry_body(item)
            link = _entry_link(item)
            blob = f"{title} {body}"

            if not title:
                continue
            if not INTEREST.search(blob):
                stats["not_interesting"] += 1
                continue
            if EDITORIAL.search(title) or EDITORIAL.search(link):
                stats["editorial"] += 1
                continue

            claim_urls = extract_claim_urls(body, link)

            # The decisive filter: an actionable lead needs EITHER a claim-like URL we
            # could hand to a human, OR announcement phrasing in the title/body.
            # Without this, "airdrop" in a headline is treated as a live drop.
            announced = bool(ANNOUNCE.search(title) or ANNOUNCE.search(blob))
            if not claim_urls and not announced:
                stats["no_signal"] += 1
                continue

            chain_key = guess_chain(blob)
            stats["kept"] += 1
            candidates.append(
                ProjectCandidate(
                    slug=slugify(f"{feed.get('name', 'feed')}-{title}"),
                    name=title[:200],
                    source_plugin=self.name,
                    source_url=link or url,
                    chain_key=chain_key,
                    docs_url=link or url,
                    faucet_urls=claim_urls,
                    notes=(
                        f"Unverified lead from {feed.get('name', url)} "
                        f"({'announcement phrasing' if announced else 'claim URL found'}). "
                        "Aggregator/news content — confirm the project's own announcement "
                        "before treating anything here as real, and never assume automation "
                        "is permitted."
                    ),
                    raw={
                        "feed": url,
                        "feed_name": feed.get("name"),
                        "published": item.findtext("pubDate")
                        or item.findtext("atom:updated", default="", namespaces=ATOM),
                        "chain_guess": chain_key,
                        "claim_urls": claim_urls,
                        "matched_announcement": announced,
                        "automation_allowed": "UNKNOWN",
                    },
                )
            )
        log.info("airdrop_feed_parsed", extra={"feed": feed.get("name"), **stats})
        return candidates

