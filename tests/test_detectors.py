"""Detector tests: each detector must fire on realistic markup and stay quiet otherwise."""

from __future__ import annotations

from app.faucet.detectors import (
    detect_amount,
    detect_captcha,
    detect_cloudflare,
    detect_cooldown,
    detect_empty,
    detect_login,
    detect_offline,
    detect_wallet_input,
    strip_html,
)


def test_strip_html_removes_scripts_and_tags():
    raw = "<html><script>var x=1;</script><body><p>Hello <b>world</b></p></body></html>"
    out = strip_html(raw)
    assert "var x" not in out
    assert "Hello" in out and "world" in out
    assert "<" not in out


def test_captcha_detection_variants():
    assert detect_captcha('<div class="h-captcha">', "").matched
    assert detect_captcha('<div class="g-recaptcha">', "").meta["type"] == "recaptcha"
    assert detect_captcha('<div class="cf-turnstile">', "").meta["type"] == "turnstile"
    h = detect_captcha("<p>Please verify you are human</p>", "Please verify you are human")
    assert h.matched and h.meta["type"] == "generic"


def test_captcha_absent():
    hit = detect_captcha("<form><input name='address'></form>", "Enter your address")
    assert not hit.matched


def test_cloudflare_by_body_and_header():
    assert detect_cloudflare("<h1>Just a moment...</h1>", {}, 503).matched
    assert detect_cloudflare("ok", {"cf-mitigated": "challenge"}, 403).matched
    assert detect_cloudflare("ok", {"server": "cloudflare"}, 403).matched
    assert not detect_cloudflare("hello", {"server": "nginx"}, 200).matched


def test_embedded_cloudflare_challenge_on_http_200():
    """Regression: faucet.quicknode.com serves a normal 200 page whose form is just
    {wallet, authToken}, but injects the Cloudflare challenge platform script that fills
    that token. Header-only detection missed it entirely, so it was reported as an
    ordinary claimable faucet."""
    html = (
        '<form action="/x" method="post">'
        '<input name="wallet" type="text"><input name="authToken" type="hidden" value="">'
        "</form><script>var a=document.createElement('script');"
        "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>"
    )
    hit = detect_cloudflare(html, {"server": "cloudflare"}, 200)
    assert hit.matched
    assert hit.meta.get("kind") == "embedded_js_challenge"


def test_turnstile_detected_in_body():
    html = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
    hit = detect_cloudflare(html, {}, 200)
    assert hit.matched
    assert hit.meta.get("kind") == "turnstile"


def test_plain_cdn_cgi_is_not_flagged():
    # /cdn-cgi/l/email-protection is obfuscation, not a bot challenge. Do not cry wolf.
    html = '<a href="/cdn-cgi/l/email-protection#abc">mail</a>'
    assert not detect_cloudflare(html, {"server": "nginx"}, 200).matched


def test_login_providers():
    assert detect_login("<a>Sign in with GitHub</a>").meta["provider"] == "github"
    assert detect_login("<a>Login with Discord</a>").meta["provider"] == "discord"
    assert detect_login("You must be logged in").meta["provider"] == "generic"
    assert not detect_login("welcome").matched


def test_wallet_input_forms():
    assert detect_wallet_input("<input name='walletAddress' type='text'>").matched
    assert detect_wallet_input("<input placeholder='0x1234567890'>").matched
    assert detect_wallet_input("Enter your wallet address").matched
    assert not detect_wallet_input("<p>no form here</p>").matched


def test_cooldown_extracts_seconds():
    hit = detect_cooldown("You can claim again in 24 hours")
    assert hit.matched
    assert hit.meta["cooldown_seconds"] == 86400

    hit2 = detect_cooldown("Cooldown active, wait 30 minutes")
    assert hit2.matched and hit2.meta["cooldown_seconds"] == 1800


def test_empty_and_offline():
    assert detect_empty("The faucet is empty right now").matched
    assert detect_empty("out of funds").matched
    assert detect_offline("We are under maintenance", 200).matched
    assert detect_offline("", 502).matched          # 5xx alone is enough
    assert not detect_offline("all good", 200).matched


def test_amount_parsing():
    hit = detect_amount("You will receive 0.05 ETH per claim")
    assert hit.matched
    assert hit.meta["amounts"][0] == {"value": "0.05", "unit": "ETH"}


def test_amount_handles_commas():
    hit = detect_amount("Claim 1,5 POL")
    assert hit.meta["amounts"][0]["value"] == "1.5"

