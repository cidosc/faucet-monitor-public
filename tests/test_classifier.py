"""Classifier tests.

The rules that matter most:
  * a network failure is UNKNOWN, NOT OFFLINE (we cannot blame the faucet for our link)
  * Cloudflare/captcha are never retried around
  * low confidence is downgraded to UNKNOWN
"""

from __future__ import annotations

from app.faucet.classifier import (
    AVAILABLE,
    CAPTCHA,
    COOLDOWN,
    EMPTY,
    OFFLINE,
    REQUIRE_MANUAL,
    UNKNOWN,
    apply_confidence_floor,
    classify,
)
from app.http import FetchResult


def _r(**kw) -> FetchResult:
    base = {"url": "https://example.test/faucet"}
    base.update(kw)
    return FetchResult(**base)


def test_network_error_is_unknown_not_offline():
    c = classify(_r(status=None, error="ConnectTimeout: "))
    assert c.status == UNKNOWN
    assert "no_response" in c.reasons[0]


def test_robots_denied_is_unknown():
    c = classify(_r(robots_denied=True))
    assert c.status == UNKNOWN
    assert c.confidence == 1.0


def test_404_is_offline_url_gone():
    c = classify(_r(status=404, text="not found"))
    assert c.status == OFFLINE
    assert "url_gone" in c.reasons[0]


def test_5xx_is_offline():
    c = classify(_r(status=503, text="oops"))
    assert c.status == OFFLINE


def test_cloudflare_wins_over_available_looking_form():
    html = "<h1>Just a moment...</h1><input name='address'>"
    c = classify(_r(status=503, text=html))
    assert c.status == CAPTCHA
    assert c.cloudflare_challenge is True


def test_captcha_detected():
    html = "<div class='g-recaptcha'></div><input name='address'>"
    c = classify(_r(status=200, text=html))
    assert c.status == CAPTCHA
    assert c.captcha_type == "recaptcha"
    assert c.requires_captcha is True


def test_403_without_challenge_is_require_manual():
    c = classify(_r(status=403, text="forbidden", block_reason="http_403 (nginx)"))
    assert c.status == REQUIRE_MANUAL


def test_login_gate_is_require_manual():
    c = classify(_r(status=200, text="<a>Sign in with GitHub</a>"))
    assert c.status == REQUIRE_MANUAL
    assert c.login_provider == "github"


def test_empty_beats_cooldown():
    c = classify(_r(status=200, text="The faucet is empty. Cooldown 24 hours."))
    assert c.status == EMPTY


def test_cooldown_detected():
    c = classify(_r(status=200, text="You can claim again in 12 hours"))
    assert c.status == COOLDOWN
    assert c.cooldown_seconds == 43200


def test_available_requires_wallet_form_and_is_capped():
    html = "<form><input name='walletAddress' placeholder='0x...'></form>"
    c = classify(_r(status=200, text=html))
    assert c.status == AVAILABLE
    assert c.requires_wallet_address is True
    # deliberately capped: we cannot confirm dispensing without actually claiming
    assert c.confidence <= 0.70


def test_reachable_without_wallet_form_is_unknown():
    c = classify(_r(status=200, text="<p>Welcome to our docs</p>"))
    assert c.status == UNKNOWN
    assert "no_wallet_form" in c.reasons[0]


def test_confidence_floor_downgrades():
    c = classify(_r(status=200, text="cooldown"))   # cooldown detector confidence 0.75
    assert c.status == COOLDOWN
    c.confidence = 0.10
    apply_confidence_floor(c)
    assert c.status == UNKNOWN

