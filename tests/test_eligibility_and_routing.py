"""Eligibility rollup + HTTP route-selection tests."""

from __future__ import annotations

from app.eligibility.engine import (
    FAIL,
    NOT_ELIGIBLE,
    PARTIAL,
    PASS,
    READY,
    UNKNOWN,
    CheckOutcome,
    rollup,
)
from app.http import Fetcher


class TestRollup:
    def test_all_pass_is_ready(self):
        status, p, f, u = rollup([CheckOutcome(PASS), CheckOutcome(PASS)], [True, True])
        assert status == READY and (p, f, u) == (2, 0, 0)

    def test_mix_of_pass_and_unknown_is_partial(self):
        """Matches the requested example: 2 checks pass, one unknown."""
        status, p, f, u = rollup(
            [CheckOutcome(PASS), CheckOutcome(PASS), CheckOutcome(UNKNOWN)], [True] * 3
        )
        assert status == PARTIAL and (p, f, u) == (2, 0, 1)

    def test_pass_plus_fail_is_partial(self):
        status, *_ = rollup([CheckOutcome(PASS), CheckOutcome(FAIL)], [True, True])
        assert status == PARTIAL

    def test_nothing_passes_is_not_eligible(self):
        status, *_ = rollup([CheckOutcome(FAIL), CheckOutcome(FAIL)], [True, True])
        assert status == NOT_ELIGIBLE

    def test_all_unknown_is_unknown(self):
        status, *_ = rollup([CheckOutcome(UNKNOWN), CheckOutcome(UNKNOWN)], [True, True])
        assert status == UNKNOWN

    def test_optional_requirements_are_excluded(self):
        # second check is not required -> must not affect the verdict
        status, p, f, u = rollup(
            [CheckOutcome(PASS), CheckOutcome(FAIL)], [True, False]
        )
        assert status == READY and (p, f, u) == (1, 0, 0)

    def test_empty_input_is_unknown(self):
        assert rollup([], [])[0] == UNKNOWN


class TestRouteSelection:
    def test_configured_proxy_only_hosts_match_by_suffix(self, fresh_settings):
        s = fresh_settings(HTTP_PROXY_ONLY_HOSTS="example.com,api.test.org")
        assert s.host_prefers_proxy("example.com")
        assert s.host_prefers_proxy("www.example.com")
        assert s.host_prefers_proxy("api.test.org")
        assert not s.host_prefers_proxy("notexample.com")
        assert not s.host_prefers_proxy("")

    def test_host_prefers_proxy_ignores_port(self, fresh_settings):
        s = fresh_settings(HTTP_PROXY_ONLY_HOSTS="example.com")
        assert s.host_prefers_proxy("example.com:8443")

    def test_route_plan_without_proxy_is_direct_only(self, fresh_settings):
        fresh_settings(HTTP_PROXY_URL="", HTTP_PROXY_FALLBACK="false")
        f = Fetcher()
        assert [r[0] for r in f._plan] == ["direct"]

    def test_route_plan_with_proxy_has_fallback(self, fresh_settings):
        fresh_settings(HTTP_PROXY_URL="http://proxy:7890", HTTP_PROXY_FALLBACK="true")
        f = Fetcher()
        assert [r[0] for r in f._plan] == ["direct", "proxy"]

    def test_fallback_can_be_disabled(self, fresh_settings):
        fresh_settings(HTTP_PROXY_URL="http://proxy:7890", HTTP_PROXY_FALLBACK="false")
        f = Fetcher()
        assert [r[0] for r in f._plan] == ["direct"]

    def test_learned_proxy_host_is_recorded(self, fresh_settings):
        fresh_settings(HTTP_PROXY_URL="http://proxy:7890")
        assert "blocked.test" not in Fetcher._prefer_proxy
        Fetcher._prefer_proxy.add("blocked.test")
        assert "blocked.test" in Fetcher._prefer_proxy
        Fetcher._prefer_proxy.discard("blocked.test")

