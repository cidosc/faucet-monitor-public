"""Claim readiness logic.

The adapter is faked so these tests are deterministic and never touch the network. What
they pin down is the decision table: window handling, the claimed-existence check, the
simulate-then-classify path, and the guarantee that a revert is never mistaken for
success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chains.base import ChainProbeResult
from app.claims import readiness as R


class FakeAdapter:
    """Records what was asked; returns scripted answers. Tracks that nothing was sent."""

    def __init__(self, *, call_ok=True, call_value="0x" + "0" * 64, call_error=None,
                 gas=21000, gas_price=10**9, estimate_ok=True):
        self.calls: list[tuple[str, str, str | None]] = []
        self.estimates = 0
        self.configured = True
        self._call_ok = call_ok
        self._call_value = call_value
        self._call_error = call_error
        self._gas = gas
        self._gas_price = gas_price
        self._estimate_ok = estimate_ok

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, to, data, block="latest", sender=None):
        self.calls.append((to, data, sender))
        if self._call_ok:
            return ChainProbeResult(True, self._call_value, evidence={"method": "eth_call"})
        return ChainProbeResult(False, error=self._call_error, evidence={"method": "eth_call"})

    async def estimate_gas(self, to, data, sender=None, value=None):
        self.estimates += 1
        if not self._estimate_ok:
            return ChainProbeResult(False, error="estimate failed")
        return ChainProbeResult(True, self._gas)

    async def gas_price(self):
        return ChainProbeResult(True, self._gas_price)


def _wallet(address="0x1111111111111111111111111111111111111111"):
    return SimpleNamespace(id=1, label="main", address=address, enabled=True)


def _chain(is_testnet=True):
    return SimpleNamespace(id=1, key="eth-sepolia", native_symbol="ETH",
                           is_testnet=is_testnet, rpc_url_env="RPC_ETH_SEPOLIA")


def _target(**kw):
    base = dict(
        id=1, label="Season 1", kind="CONTRACT_CLAIM",
        contract_address="0x00000000000000000000000000000000000000c1",
        claim_signature="claim()", claimed_signature=None, claimed_arg="address",
        proof_source_url=None, proof_json_path=None, amount_field=None, proof_field=None,
        window_start=None, window_end=None, human_verified=False, claim_url="https://x.test/claim",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def patch_adapter(monkeypatch):
    holder = {}

    def _install(adapter):
        holder["a"] = adapter
        monkeypatch.setattr(R, "make_adapter", lambda chain: adapter)
        return adapter

    return _install


async def _run(target, adapter, chain=None, wallet=None):
    return await R.check_readiness(
        None, wallet or _wallet(), target, chain or _chain()
    )


class TestWindow:
    async def test_before_window(self, patch_adapter):
        patch_adapter(FakeAdapter())
        t = _target(window_start=datetime.now(UTC) + timedelta(days=1))
        res = await _run(t, None)
        assert res.status == R.WINDOW_NOT_OPEN
        assert res.window_open is False

    async def test_after_window(self, patch_adapter):
        patch_adapter(FakeAdapter())
        t = _target(window_end=datetime.now(UTC) - timedelta(days=1))
        res = await _run(t, None)
        assert res.status == R.WINDOW_CLOSED

    async def test_inside_window_proceeds(self, patch_adapter):
        a = patch_adapter(FakeAdapter())
        t = _target(window_start=datetime.now(UTC) - timedelta(days=1),
                    window_end=datetime.now(UTC) + timedelta(days=1))
        res = await _run(t, a)
        assert res.status == R.READY


class TestNonSimulatable:
    async def test_manual_only_is_unsimulated(self, patch_adapter):
        patch_adapter(FakeAdapter())
        res = await _run(_target(kind="MANUAL_ONLY", claim_signature=None), None)
        assert res.status == R.UNSIMULATED
        # must not have attempted anything
        assert res.calldata is None

    async def test_missing_signature_is_unsimulated(self, patch_adapter):
        patch_adapter(FakeAdapter())
        res = await _run(_target(claim_signature=None), None)
        assert res.status == R.UNSIMULATED


class TestAlreadyClaimed:
    async def test_existence_check_short_circuits(self, patch_adapter):
        a = FakeAdapter(call_value="0x" + "0" * 63 + "1")  # isClaimed -> true
        patch_adapter(a)
        res = await _run(_target(claimed_signature="isClaimed(address)"), a)
        assert res.status == R.ALREADY_CLAIMED
        assert res.already_claimed is True
        # Only the existence check ran; the claim was NOT simulated.
        assert a.estimates == 0

    async def test_not_claimed_continues_to_simulation(self, patch_adapter):
        a = FakeAdapter()
        patch_adapter(a)
        res = await _run(_target(claimed_signature="isClaimed(address)"), a)
        assert res.already_claimed is False
        assert res.status == R.READY

    async def test_revert_saying_already_claimed_is_classified(self, patch_adapter):
        # Error(string) encoding of "Already claimed"
        msg = b"Already claimed"
        payload = "0x08c379a0" + f"{32:064x}" + f"{len(msg):064x}" + msg.hex().ljust(64, "0")
        a = FakeAdapter(call_ok=False, call_error=payload)
        patch_adapter(a)
        res = await _run(_target(), a)
        assert res.status == R.ALREADY_CLAIMED
        assert res.already_claimed is True


class TestSimulation:
    async def test_ready_reports_gas_and_cost(self, patch_adapter):
        a = patch_adapter(FakeAdapter(gas=100_000, gas_price=20 * 10**9))
        res = await _run(_target(), a)
        assert res.status == R.READY
        assert res.simulated_ok is True
        assert res.gas_estimate == Decimal(100_000)
        assert res.gas_price_wei == Decimal(20 * 10**9)
        # price provider defaults to 0 => unknown price => USD must be None, not 0.00
        assert res.estimated_usd is None

    async def test_revert_other_than_already_claimed_is_not_eligible(self, patch_adapter):
        a = patch_adapter(FakeAdapter(call_ok=False, call_error="execution reverted: Invalid proof"))
        res = await _run(_target(), a)
        assert res.status == R.NOT_ELIGIBLE
        assert res.revert_reason == "Invalid proof"
        assert res.simulated_ok is False
        # A failed simulation must never be reported as ready.
        assert res.status != R.READY

    async def test_calldata_matches_the_configured_signature(self, patch_adapter):
        from app.claims.abi import selector

        a = patch_adapter(FakeAdapter())
        res = await _run(_target(claim_signature="claim()"), a)
        assert res.calldata == selector("claim()")
        assert res.to_address == "0x00000000000000000000000000000000000000c1"
        # The simulation was performed with the wallet as `from`.
        assert a.calls[0][2] == "0x1111111111111111111111111111111111111111"

    async def test_estimate_failure_still_reports_ready(self, patch_adapter):
        # Simulation succeeded but gas could not be estimated: the claim IS available,
        # we just don't know the cost. That is READY with unknown cost, not a failure.
        a = patch_adapter(FakeAdapter(estimate_ok=False))
        res = await _run(_target(), a)
        assert res.status == R.READY
        assert res.gas_estimate is None
        assert res.estimated_usd is None


class TestNotificationPolicy:
    def test_alerts_on_becoming_ready(self):
        assert R.should_notify(None, R.READY) is True

    def test_no_alert_on_steady_state(self):
        assert R.should_notify(R.READY, R.READY) is False

    def test_alerts_on_transition_between_states(self):
        assert R.should_notify(R.NOT_ELIGIBLE, R.READY) is True
        assert R.should_notify(R.READY, R.ALREADY_CLAIMED) is True

    def test_silent_on_inconclusive_states(self):
        assert R.should_notify(R.READY, R.UNKNOWN) is False
        assert R.should_notify(R.READY, R.UNSIMULATED) is False

