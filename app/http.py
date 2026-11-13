"""Outbound HTTP with the project's fetch policy baked in.

Guarantees, enforced centrally so no call site can forget them:
  * explicit connect/read timeouts
  * exponential backoff with jitter, bounded retry count
  * a per-host sliding-window rate limit (default 6 requests/minute/host — never hammer)
  * a descriptive User-Agent
  * robots.txt is honoured when RESPECT_ROBOTS_TXT=true
  * TLS verification on by default

Route selection
---------------
The fetcher tries direct connections first, then an optional configured proxy.
Only transport-level failures fall through to the proxy, and the
route that succeeded is recorded in the result/evidence. A definitive HTTP answer
(200/403/404/...) from any route is final and is never retried elsewhere.

Hard limits on behaviour: this module only performs ordinary GET/HEAD requests and
reports what the server returned. It does NOT solve, bypass, or retry around CAPTCHAs,
Cloudflare challenges, WAF blocks or rate limits. A 403/503 challenge is a final answer,
recorded as such — never something to work around.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# Status codes that mean "the server is temporarily struggling" and are worth retrying.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Status codes that indicate a bot challenge / access control. We record these and stop.
# We never attempt to defeat them.
CHALLENGE_STATUS = {401, 403, 406, 451}

# Transport failures that justify trying the next route (usually: DNS blocked).
_FALLBACK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    blocked: bool = False          # a challenge/access-control response was seen
    block_reason: str | None = None
    robots_denied: bool = False
    route: str = "direct"          # which route actually produced this result
    route_attempts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and self.error is None


class HostRateLimiter:
    """Sliding-window limiter, one bucket per host."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str) -> float:
        """Block until this host is under its budget. Returns seconds waited."""
        waited = 0.0
        async with self._locks[host]:
            while True:
                now = time.monotonic()
                bucket = self._hits[host]
                while bucket and now - bucket[0] > 60.0:
                    bucket.popleft()
                if len(bucket) < self.per_minute:
                    bucket.append(now)
                    return waited
                sleep_for = min(60.0 - (now - bucket[0]) + 0.05, 30.0)
                waited += sleep_for
                await asyncio.sleep(sleep_for)


class RobotsCache:
    """Caches robots.txt per origin per route."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def allowed(self, url: str) -> bool:
        if not settings.respect_robots_txt:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        async with self._locks[origin]:
            if origin not in self._cache:
                self._cache[origin] = await self._load(origin)
            parser = self._cache[origin]

        if parser is None:
            return True
        try:
            return parser.can_fetch(settings.http_user_agent, url)
        except Exception:  # noqa: BLE001
            return True

    async def _load(self, origin: str) -> RobotFileParser | None:
        try:
            resp = await self._client.get(f"{origin}/robots.txt")
            if resp.status_code != 200 or not resp.text:
                return None
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except Exception as exc:  # noqa: BLE001
            log.debug("robots_fetch_failed", extra={"origin": origin, "error": str(exc)})
            return None


@dataclass
class _Route:
    label: str
    client: httpx.AsyncClient
    robots: RobotsCache


class Fetcher:
    """Async HTTP fetcher with optional proxy fallback. Use as an async context manager.

    Host-level route memory: a host that once needed the proxy is remembered for the
    process lifetime and tried through the proxy FIRST next time. Without this, every
    request to an unreachable host pays the direct-connect timeout again.
    """

    # host -> prefers proxy. Module-level so it is shared across Fetcher instances.
    _prefer_proxy: set[str] = set()
    _prefer_direct: set[str] = set()

    def __init__(
        self,
        *,
        proxy: str | None = None,
        fallback_proxy: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._routes: list[_Route] = []
        self._limiter = HostRateLimiter(settings.rate_limit_per_host_per_min)
        self._sem = asyncio.Semaphore(settings.http_max_concurrent)
        self._ua = user_agent or settings.http_user_agent

        # Explicit proxy= wins; otherwise honour the configured fallback.
        primary_proxy = proxy or None
        fb = fallback_proxy if fallback_proxy is not None else (settings.http_proxy_url or None)
        if not settings.http_proxy_fallback:
            fb = None

        self._plan: list[tuple[str, str | None]] = [("direct", primary_proxy)]
        if fb and fb != primary_proxy:
            self._plan.append(("proxy", fb))

    async def __aenter__(self) -> Fetcher:
        timeout = httpx.Timeout(
            settings.http_timeout_seconds, connect=settings.http_connect_timeout_seconds
        )
        for label, proxy_url in self._plan:
            kwargs: dict[str, object] = {
                "timeout": timeout,
                "follow_redirects": True,
                "max_redirects": 5,
                "verify": settings.verify_tls,
                "headers": {
                    "User-Agent": self._ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml,"
                              "application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "limits": httpx.Limits(
                    max_connections=settings.http_max_concurrent * 2,
                    max_keepalive_connections=settings.http_max_concurrent,
                ),
            }
            if proxy_url:
                kwargs["proxy"] = proxy_url
            client = httpx.AsyncClient(**kwargs)
            self._routes.append(_Route(label, client, RobotsCache(client)))
        return self

    async def __aexit__(self, *exc: object) -> None:
        for route in self._routes:
            await route.client.aclose()
        self._routes.clear()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._routes:
            raise RuntimeError("Fetcher must be used as an async context manager")
        return self._routes[0].client

    async def get(
        self,
        url: str,
        *,
        check_robots: bool = True,
        accept: str | None = None,
        max_bytes: int = 2_000_000,
    ) -> FetchResult:
        started = time.monotonic()
        result = FetchResult(url=url)
        host = urlparse(url).netloc or ""

        # Reorder routes based on what worked for this host before, or on the
        # configured proxy-only list (avoids the direct-timeout penalty entirely).
        routes = self._routes
        if host and len(routes) > 1 and (host in self._prefer_proxy or settings.host_prefers_proxy(host)):
            routes = list(reversed(routes))

        for route in routes:
            if check_robots and not await route.robots.allowed(url):
                result.robots_denied = True
                result.error = "disallowed_by_robots_txt"
                result.route = route.label
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                log.info("robots_denied", extra={"url": url, "route": route.label})
                return result

            result.route_attempts.append(route.label)
            is_last = route is routes[-1]
            # Non-final routes get a single quick attempt: a poisoned/blocked direct
            # connection should hand over to the proxy promptly instead of burning the
            # full retry budget with backoff (which cost ~38s before this change).
            route_attempts = settings.http_max_retries if is_last else 1
            outcome = await self._attempt(
                route, url, accept=accept, max_bytes=max_bytes, attempts=route_attempts
            )

            # A definitive HTTP answer from any route is final.
            if outcome.status is not None:
                outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
                outcome.route_attempts = list(result.route_attempts)
                outcome.route = route.label
                if host:
                    if route.label == "proxy" and len(result.route_attempts) > 1:
                        self._prefer_proxy.add(host)
                        log.info(
                            "route_learned",
                            extra={"host": host, "prefer": "proxy",
                                   "status": outcome.status, "first_error": result.error},
                        )
                    elif len(result.route_attempts) == 1:
                        self._prefer_direct.add(host)
                return outcome

            # Transport-level failure: remember it and try the next route.
            result.error = outcome.error
            result.attempts = outcome.attempts
            if not self._is_fallback_worthy(outcome.error):
                break

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "fetch_failed",
            extra={"url": url, "error": result.error, "routes": result.route_attempts},
        )
        return result

    @staticmethod
    def _is_fallback_worthy(error: str | None) -> bool:
        if not error:
            return False
        return any(
            name in error
            for name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError")
        )

    async def _attempt(
        self,
        route: _Route,
        url: str,
        *,
        accept: str | None,
        max_bytes: int,
        attempts: int,
    ) -> FetchResult:
        result = FetchResult(url=url)
        host = urlparse(url).netloc or "unknown"
        headers = {"Accept": accept} if accept else None

        for attempt in range(1, max(1, attempts) + 1):
            result.attempts = attempt
            await self._limiter.acquire(host)
            try:
                async with self._sem:
                    async with route.client.stream("GET", url, headers=headers) as resp:
                        result.status = resp.status_code
                        result.final_url = str(resp.url)
                        result.headers = {k.lower(): v for k, v in resp.headers.items()}

                        if resp.status_code in CHALLENGE_STATUS:
                            result.blocked = True
                            result.block_reason = _challenge_reason(resp.status_code, result.headers)
                            log.info(
                                "access_control_response",
                                extra={"url": url, "status": resp.status_code,
                                       "reason": result.block_reason, "route": route.label},
                            )
                            return result   # final answer, never worked around

                        if resp.status_code in RETRYABLE_STATUS:
                            raise httpx.HTTPStatusError(
                                f"retryable status {resp.status_code}",
                                request=resp.request,
                                response=resp,
                            )

                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                break
                            chunks.append(chunk)
                        result.text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                        return result

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                result.status = status
                result.error = f"http_{status}"
                if attempt >= attempts:
                    break
                await self._backoff(attempt, host, f"status {status}")
            except _FALLBACK_ERRORS as exc:
                result.status = None
                result.error = f"{type(exc).__name__}: {exc}".strip()
                if attempt >= attempts:
                    break
                await self._backoff(attempt, host, type(exc).__name__)
            except Exception as exc:  # noqa: BLE001
                result.status = None
                result.error = f"{type(exc).__name__}: {exc}"
                break

        # Retries exhausted without a definitive answer.
        result.status = None
        return result

    async def _backoff(self, attempt: int, host: str, reason: str) -> None:
        delay = min(settings.http_backoff_base**attempt, settings.http_backoff_max_seconds)
        delay *= 0.7 + random.random() * 0.6  # jitter
        log.info("fetch_retry", extra={"host": host, "attempt": attempt,
                                       "reason": reason, "delay_s": round(delay, 2)})
        await asyncio.sleep(delay)


def _challenge_reason(status: int, headers: dict[str, str]) -> str:
    if "cf-mitigated" in headers:
        return "cloudflare_mitigated"
    if headers.get("server", "").lower().startswith("cloudflare") and status == 403:
        return "cloudflare_block"
    server = headers.get("server", "")
    return f"http_{status}" + (f" ({server})" if server else "")


async def fetch(url: str, *, proxy: str | None = None, check_robots: bool = True) -> FetchResult:
    """One-shot convenience wrapper."""
    async with Fetcher(proxy=proxy) as f:
        return await f.get(url, check_robots=check_robots)

