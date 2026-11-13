"""Evidence-based detectors for faucet pages.

Each detector returns a `DetectorHit` carrying the matched regex and the surrounding
text snippet, so every status decision is auditable after the fact ("why did it say
COOLDOWN on Tuesday?").

Design rule: detectors describe *observations*. They never decide policy. In particular
`automation_policy` only surfaces hints for a human to read — it can never flip
`automation_allowed` to ALLOWED.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from typing import Any

# ────────────────────────────── helpers ──────────────────────────────

_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def strip_html(raw: str) -> str:
    """Remove script/style, then tags, then collapse whitespace. For text matching."""
    without_scripts = _TAG_RE.sub(" ", raw)
    no_tags = _ANY_TAG.sub(" ", without_scripts)
    unescaped = html_mod.unescape(no_tags)
    return _WS.sub(" ", unescaped).strip()


def snippet(text: str, match: re.Match[str], width: int = 90) -> str:
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return _WS.sub(" ", text[start:end]).strip()


@dataclass
class DetectorHit:
    detector: str
    matched: bool
    confidence: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "matched": self.matched,
            "confidence": round(self.confidence, 2),
            "meta": self.meta,
            "evidence": self.evidence[:5],
        }


def _hit(
    name: str, text: str, patterns: list[str], confidence: float, meta: dict[str, Any] | None = None
) -> DetectorHit:
    found: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            found.append(f"{pat} :: {snippet(text, m)}")
            if len(found) >= 5:
                break
        if len(found) >= 5:
            break
    return DetectorHit(
        detector=name,
        matched=bool(found),
        confidence=confidence if found else 0.0,
        meta=meta or {},
        evidence=found,
    )


# ────────────────────────────── patterns ──────────────────────────────

CAPTCHA_PATTERNS = {
    "hcaptcha": [r"h-?captcha"],
    "recaptcha": [r"g-?recaptcha", r"google\.com/recaptcha", r"recaptcha/api\.js"],
    "turnstile": [r"cf-turnstile", r"challenges\.cloudflare\.com/turnstile"],
    "geetest": [r"geetest"],
    "generic": [r"verify (?:that )?you are (?:a )?human", r"i(?:'|&#39;|&rsquo;)?m not a robot",
                r"complete the captcha", r"anti-?bot (?:check|verification)"],
}

CLOUDFLARE_PATTERNS = [
    r"just a moment",
    r"checking your browser",
    r"cf-chl-",
    r"cf_chl_opt",
    r"__cf_chl",
    r"enable javascript and cookies to continue",
    r"attention required! \| cloudflare",
    r"ray id:",
    # Embedded Cloudflare bot protection. This is the important one: the page returns a
    # perfectly normal HTTP 200, so header-only detection misses it entirely. The
    # challenge platform injects a script that solves a JS challenge and fills a hidden
    # token field which is then submitted with the form.
    #   found on faucet.quicknode.com, whose form is otherwise just {wallet, authToken}
    r"/cdn-cgi/challenge-platform/",
    r"challenge-platform/scripts/jsd",
    r"turnstile/v0/api\.js",
    r"challenges\.cloudflare\.com",
    r"window\._cf_chl_opt",
]

LOGIN_PATTERNS = {
    "github": [r"sign in with github", r"login with github", r"github\.com/login/oauth",
               r"continue with github"],
    "discord": [r"sign in with discord", r"login with discord", r"discord\.com/oauth2",
                r"continue with discord", r"join (?:our|the) discord"],
    "google": [r"sign in with google", r"continue with google", r"accounts\.google\.com/o/oauth2"],
    "twitter": [r"sign in with (?:twitter|x)\b", r"continue with (?:twitter|x)\b",
                r"api\.twitter\.com/oauth"],
    "walletconnect": [r"walletconnect", r"connect wallet to continue"],
    "generic": [r"you must (?:be )?(?:log|sign)ged? in", r"please (?:log|sign) in to continue",
                r"authentication required", r"sign in to (?:continue|claim)"],
}

WALLET_INPUT_PATTERNS = [
    r"<input[^>]+(?:name|id)\s*=\s*[\"'][^\"']*(?:address|wallet|addr)[^\"']*[\"']",
    r"placeholder\s*=\s*[\"'][^\"']*0x[a-f0-9]{6,}",
    r"placeholder\s*=\s*[\"'][^\"']*(?:wallet|address)[^\"']*[\"']",
    r"enter your (?:wallet )?address",
    r"paste your (?:wallet )?address",
    r"0x[a-fA-F0-9]{40}",
]

COOLDOWN_PATTERNS = [
    r"cooldown",
    r"cool-?down",
    r"you can claim again",
    r"next claim (?:available )?in",
    r"come back (?:in|later|tomorrow)",
    r"already claimed",
    r"claim(?:ed)? (?:again )?(?:in|after) \d+",
    r"wait \d+ (?:seconds?|minutes?|hours?|days?)",
    r"every \d+ (?:seconds?|minutes?|hours?|days?)",
    r"once (?:per|every) \d+ (?:hours?|days?)",
    r"daily limit reached",
    r"rate limit(?:ed)?",
]

EMPTY_PATTERNS = [
    r"faucet is empty",
    r"out of funds",
    r"insufficient (?:funds|balance)",
    r"no funds (?:left|available)",
    r"faucet (?:has )?(?:run|ran) dry",
    r"empty balance",
    r"we(?:'| a)?re out of",
]

OFFLINE_PATTERNS = [
    r"under maintenance",
    r"temporarily unavailable",
    r"scheduled maintenance",
    r"service (?:is )?(?:down|offline)",
    r"be right back",
    r"coming soon",
    r"under construction",
    r"faucet (?:is )?(?:paused|disabled|closed)",
]

AMOUNT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(ETH|WETH|POL|MATIC|BNB|AVAX|SOL|FTM|CELO|t?USDC|t?USDT|DAI|TEST\w*|TOKENS?|CREDITS?|"
    r"SEP|t?ETH|GETH|tBNB)\b",
    re.IGNORECASE,
)

AUTOMATION_HINT_PATTERNS = [
    r"automated (?:scripts?|tools?|requests?|access)",
    r"(?:bots?|crawlers?|scrapers?)\s+(?:are|is)\s+(?:not\s+)?(?:allowed|permitted|prohibited)",
    r"no (?:automation|bots?|scripts?)",
    r"sybil",
    r"multiple accounts?",
    r"terms of (?:service|use)",
    r"prohibited (?:use|activity)",
    r"abuse of the faucet",
]

DURATION_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week)s?", re.IGNORECASE
)
_DURATION_UNITS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
}


# ────────────────────────────── detectors ──────────────────────────────


def detect_captcha(raw: str, text: str) -> DetectorHit:
    for kind, patterns in CAPTCHA_PATTERNS.items():
        h = _hit(f"captcha:{kind}", raw, patterns, 0.95)
        if h.matched:
            return DetectorHit("captcha", True, 0.95, {"type": kind}, h.evidence)
    return DetectorHit("captcha", False)


def detect_cloudflare(raw: str, headers: dict[str, str], status: int | None) -> DetectorHit:
    evidence: list[str] = []
    meta: dict[str, Any] = {}

    if "cf-mitigated" in headers:
        evidence.append(f"header cf-mitigated: {headers['cf-mitigated']}")
        meta["mitigated"] = headers["cf-mitigated"]
    if headers.get("server", "").lower().startswith("cloudflare") and status in (403, 429, 503):
        evidence.append(f"server={headers.get('server')} status={status}")
        meta["server"] = headers.get("server")
        meta["status"] = status

    body_hit = _hit("cloudflare:body", raw, CLOUDFLARE_PATTERNS, 0.9)
    if body_hit.matched:
        evidence.extend(body_hit.evidence)
        meta["body_signature"] = True
        # An embedded challenge (200 + challenge-platform script) is materially different
        # from a hard block, so record which one it is. Either way it means a human must
        # complete something a plain HTTP client cannot.
        if any("/cdn-cgi/challenge-platform" in e or "challenge-platform/scripts/jsd" in e
               for e in body_hit.evidence):
            meta["kind"] = "embedded_js_challenge"
        elif any("turnstile" in e or "challenges.cloudflare.com" in e for e in body_hit.evidence):
            meta["kind"] = "turnstile"

    return DetectorHit("cloudflare", bool(evidence), 0.9 if evidence else 0.0, meta, evidence[:5])


def detect_login(raw: str) -> DetectorHit:
    for provider, patterns in LOGIN_PATTERNS.items():
        h = _hit(f"login:{provider}", raw, patterns, 0.8 if provider == "generic" else 0.9)
        if h.matched:
            return DetectorHit("login", True, h.confidence, {"provider": provider}, h.evidence)
    return DetectorHit("login", False)


def detect_wallet_input(raw: str) -> DetectorHit:
    return _hit("wallet_input", raw, WALLET_INPUT_PATTERNS, 0.85)


def detect_cooldown(text: str) -> DetectorHit:
    hit = _hit("cooldown", text, COOLDOWN_PATTERNS, 0.75)
    if hit.matched:
        # Try to pull an explicit duration out of the surrounding context.
        best: int | None = None
        for m in DURATION_RE.finditer(text):
            value = int(m.group(1))
            unit = m.group(2).lower()
            seconds = value * _DURATION_UNITS.get(unit, 0)
            if seconds and (best is None or seconds > best):
                best = seconds
        if best is not None:
            hit.meta["cooldown_seconds"] = best
    return hit


def detect_empty(text: str) -> DetectorHit:
    return _hit("empty", text, EMPTY_PATTERNS, 0.8)


def detect_offline(text: str, status: int | None) -> DetectorHit:
    hit = _hit("offline", text, OFFLINE_PATTERNS, 0.8)
    if status is not None and status >= 500:
        hit.matched = True
        hit.confidence = max(hit.confidence, 0.9)
        hit.meta["status"] = status
        hit.evidence.append(f"HTTP {status}")
    return hit


def detect_amount(text: str) -> DetectorHit:
    amounts: list[dict[str, Any]] = []
    evidence: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in AMOUNT_RE.finditer(text):
        value = m.group(1).replace(",", ".")
        unit = m.group(2).upper()
        key = (value, unit)
        if key in seen:
            continue
        seen.add(key)
        amounts.append({"value": value, "unit": unit})
        evidence.append(snippet(text, m))
        if len(amounts) >= 5:
            break
    return DetectorHit("amount", bool(amounts), 0.6 if amounts else 0.0,
                       {"amounts": amounts}, evidence)


def detect_automation_hints(text: str) -> DetectorHit:
    """Surfaces wording that MIGHT constrain automation.

    This is informational only. It never sets automation_allowed — only a human reading
    the actual Terms may do that (see app/autoclaim/policy.py).
    """
    return _hit("automation_hint", text, AUTOMATION_HINT_PATTERNS, 0.4, {"requires_human_review": True})


def run_all(
    raw: str, headers: dict[str, str] | None = None, status: int | None = None
) -> list[DetectorHit]:
    headers = headers or {}
    text = strip_html(raw)
    return [
        detect_cloudflare(raw, headers, status),
        detect_captcha(raw, text),
        detect_login(raw),
        detect_wallet_input(raw),
        detect_cooldown(text),
        detect_empty(text),
        detect_offline(text, status),
        detect_amount(text),
        detect_automation_hints(text),
    ]

