"""Policy gate tests.

These tests check that each of the nine policy gates blocks automation when its
required conditions are not met. They are not a security or compliance audit.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.autoclaim.policy import GATE_NAMES, evaluate


def _settings(fresh_settings, **overrides):
    return fresh_settings(
        GLOBAL_AUTO_CLAIM=overrides.get("global_auto_claim", "false"),
        AUTO_CLAIM_DRY_RUN=overrides.get("dry_run", "false"),
        AUTO_CLAIM_REQUIRE_ALLOWLIST=overrides.get("require_allowlist", "true"),
        AUTO_CLAIM_MAX_RETRIES=overrides.get("max_retries", 2),
        AUTO_CLAIM_MAX_USD=overrides.get("max_usd", 0),
    )


def _faucet(**kw):
    base = dict(
        id=1,
        is_official_source=True,
        is_free_resource=True,
        requires_captcha=False,
        captcha_type=None,
        cloudflare_challenge=False,
        cooldown_seconds=86400,
        auto_claim_allowlisted=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _project(**kw):
    base = dict(id=1, slug="p", chain_id=1, automation_allowed="ALLOWED",
                automation_evidence="Terms v1: 'automated use is permitted' https://x.test/tos")
    base.update(kw)
    return SimpleNamespace(**base)


def _wallet(**kw):
    base = dict(id=1, label="testnet", auto_faucet=True, is_test_wallet=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _chain(**kw):
    base = dict(id=1, key="eth-sepolia", chain_type="evm", chain_id=11155111, is_testnet=True)
    base.update(kw)
    return SimpleNamespace(**base)


async def _eval(session=None, **kw):
    return await evaluate(
        session,  # type: ignore[arg-type]
        faucet=kw.pop("faucet", _faucet()),
        project=kw.pop("project", _project()),
        wallet=kw.pop("wallet", _wallet()),
        chain=kw.pop("chain", _chain()),
        **kw,
    )


@pytest.mark.asyncio
async def test_global_switch_off_blocks_everything(fresh_settings):
    _settings(fresh_settings, global_auto_claim="false")
    d = await _eval(adapter_registered=True, adapter_tested=True)
    assert not d.allowed and d.failed_gate == 0
    # gate 0 covers the global preconditions; the specific name is what identifies which
    assert d.failed_gate_name == "global_switch"


@pytest.mark.asyncio
async def test_all_gates_pass_when_everything_is_satisfied(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(adapter_registered=True, adapter_tested=True)
    assert d.allowed, d.reasons


@pytest.mark.asyncio
async def test_dry_run_never_allows(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="true")
    d = await _eval(adapter_registered=True, adapter_tested=True)
    assert not d.allowed and d.failed_gate_name == "dry_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate,expected_gate",
    [
        (dict(faucet=_faucet(is_official_source=False)), 1),
        (dict(faucet=_faucet(is_free_resource=False)), 2),
        (dict(faucet=_faucet(requires_captcha=True, captcha_type="recaptcha")), 3),
        (dict(faucet=_faucet(cloudflare_challenge=True)), 4),
        (dict(project=_project(automation_allowed="UNKNOWN")), 5),
        (dict(project=_project(automation_allowed="FORBIDDEN")), 5),
        (dict(project=_project(automation_evidence="")), 5),
        (dict(faucet=_faucet(cooldown_seconds=None)), 8),
    ],
)
async def test_each_gate_blocks_independently(fresh_settings, mutate, expected_gate):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(adapter_registered=True, adapter_tested=True, **mutate)
    assert not d.allowed, d.reasons
    assert d.failed_gate == expected_gate, d.reasons
    assert d.failed_gate_name == GATE_NAMES[expected_gate]


@pytest.mark.asyncio
async def test_gate_6_blocks_nonzero_usd_cap(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false", max_usd=5)
    d = await _eval(adapter_registered=True, adapter_tested=True)
    assert not d.allowed and d.failed_gate == 6
    assert d.failed_gate_name == "mainnet_chain" or "MAX_USD" in d.reasons[0]


@pytest.mark.asyncio
async def test_gate_6_blocks_mainnet_chain(fresh_settings):
    """Regression guard: adding mainnet chains must not weaken "no real funds"."""
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(
        chain=_chain(key="ethereum", chain_id=1, is_testnet=False),
        adapter_registered=True,
        adapter_tested=True,
    )
    assert not d.allowed
    assert d.failed_gate == 6
    assert d.failed_gate_name == "mainnet_chain"
    assert "MAINNET" in d.reasons[0]


@pytest.mark.asyncio
async def test_gate_6_blocks_unresolved_chain(fresh_settings):
    """Fail closed: an unknown chain is not evidence of a testnet."""
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(chain=None, adapter_registered=True, adapter_tested=True)
    assert not d.allowed
    assert d.failed_gate == 6
    assert d.failed_gate_name == "no_chain"


@pytest.mark.asyncio
async def test_gate_7_blocks_when_no_adapter(fresh_settings):
    """v1 ships with an empty adapters/ dir, so this is the state every faucet is in."""
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(adapter_registered=False)
    assert not d.allowed and d.failed_gate == 7


@pytest.mark.asyncio
async def test_gate_7_blocks_untested_adapter(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(adapter_registered=True, adapter_tested=False)
    assert not d.allowed and d.failed_gate == 7


@pytest.mark.asyncio
async def test_gate_9_blocks_on_retry_exhaustion(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false", max_retries=2)
    d = await _eval(adapter_registered=True, adapter_tested=True, attempt_count=2)
    assert not d.allowed and d.failed_gate == 9


@pytest.mark.asyncio
async def test_observed_captcha_blocks_even_if_column_is_null(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(adapter_registered=True, adapter_tested=True, observed_captcha=True)
    assert not d.allowed and d.failed_gate == 3


@pytest.mark.asyncio
async def test_wallet_must_opt_in(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false")
    d = await _eval(wallet=_wallet(auto_faucet=False), adapter_registered=True, adapter_tested=True)
    assert not d.allowed and d.failed_gate == 0
    assert d.failed_gate_name == "wallet_not_opted_in"


@pytest.mark.asyncio
async def test_allowlist_required(fresh_settings):
    _settings(fresh_settings, global_auto_claim="true", dry_run="false", require_allowlist="true")
    d = await _eval(
        faucet=_faucet(auto_claim_allowlisted=False),
        adapter_registered=True,
        adapter_tested=True,
    )
    assert not d.allowed and d.failed_gate == 0
    assert d.failed_gate_name == "not_allowlisted"

