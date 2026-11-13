"""执行器：签名、广播、确认。

这是整个系统里唯一会**改变链上状态**的模块。三条铁律：

  1. **执行前重新跑一遍 9 道门槛。** 绝不信任缓存的判定结果 —— 判定和执行之间可能
     隔了几分钟，期间项目规则可能改了、captcha 可能出现了、余额可能变了。
  2. **幂等键在签名之前落库。** 崩溃/重启/重复调度都不可能产生第二笔。
  3. **私钥和签名后的原始交易绝不落盘、绝不进日志。** 签名后的 raw tx 在广播前
     等于现金。

默认 `AUTO_CLAIM_DRY_RUN=true`：会完整走完构造、估算、签名，然后**丢弃**签名结果
并报告"如果不 dry-run 会发生什么"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.autoclaim.adapters import (
    ClaimRequest,
    adapter_is_registered,
    adapter_is_tested,
    get_adapter,
)
from app.autoclaim.keys import WalletKeyError, load_signing_key, signing_available
from app.autoclaim.policy import evaluate
from app.config import settings
from app.logging_conf import get_logger
from app.models import Chain, Claim, Faucet, Project, Transaction, Wallet
from app.pricing.base import compute_usd, get_price_provider

log = get_logger(__name__)

# 保守的 gas 上限。链上真实用量通常远低于此，但这是防止估算错误把余额烧光的第一道闸。
DEFAULT_GAS_LIMIT_MULTIPLIER = Decimal("1.25")
HARD_GAS_LIMIT = 1_000_000


@dataclass
class ExecutionResult:
    ok: bool
    status: str                       # SUCCESS | FAILED | SKIPPED_POLICY | DRY_RUN
    tx_hash: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    signed_but_not_sent: bool = False


def idempotency_key(faucet_id: int, wallet_id: int, cooldown_seconds: int | None) -> str:
    """按水龙头、钱包、cooldown 周期推导。

    同一个 cooldown 窗口内重复调用会得到同一个键，UNIQUE 约束会让第二次插入失败 ——
    这就是"绝不可能重复领取"的实现方式。
    """
    window = "once"
    if cooldown_seconds and cooldown_seconds > 0:
        slot = int(datetime.now(UTC).timestamp()) // int(cooldown_seconds)
        window = str(slot)
    return f"claim:{faucet_id}:{wallet_id}:{window}"


async def _already_claimed(session: AsyncSession, key: str) -> Claim | None:
    return (
        await session.execute(select(Claim).where(Claim.idempotency_key == key))
    ).scalar_one_or_none()


async def execute_claim(
    session: AsyncSession,
    *,
    faucet: Faucet,
    project: Project,
    chain: Chain,
    wallet: Wallet,
    adapter: Any = None,
    triggered_by: str = "manual",
) -> ExecutionResult:
    """尝试对某个水龙头执行一次领取。

    返回值永远是 ExecutionResult —— 不抛异常，因为调用方（定时任务）必须能把每次
    结果记录下来，包括被拒绝的原因。
    """
    adapters = get_adapter(faucet.url) or adapter

    # ── 门槛 7 在策略里会被查一次，这里先做一次快速短路，避免无谓的准备工作 ──
    if adapters is None:
        return ExecutionResult(
            ok=False, status="SKIPPED_POLICY",
            error="没有为该水龙头注册领取适配器（gate 7）",
        )

    decision = await evaluate(
        session,
        faucet=faucet,
        project=project,
        wallet=wallet,
        chain=chain,
        adapter_registered=adapter_is_registered(faucet.url),
        adapter_tested=adapter_is_tested(faucet.url),
    )
    if not decision.allowed:
        log.info(
            "claim_blocked_by_policy",
            extra={"faucet": faucet.id, "wallet": wallet.label,
                   "gate": decision.failed_gate, "gate_name": decision.failed_gate_name},
        )
        return ExecutionResult(
            ok=False, status="SKIPPED_POLICY",
            error=f"gate {decision.failed_gate} ({decision.failed_gate_name}): "
                  + "; ".join(decision.reasons),
            detail={"gate": decision.failed_gate, "gate_name": decision.failed_gate_name},
        )

    # ── 幂等：先落库再签名 ──
    key = idempotency_key(faucet.id, wallet.id, faucet.cooldown_seconds)
    existing = await _already_claimed(session, key)
    if existing is not None:
        log.info("claim_duplicate_suppressed",
                 extra={"faucet": faucet.id, "key": key, "existing_status": existing.status})
        return ExecutionResult(
            ok=False, status="SKIPPED_DUPLICATE",
            error=f"该周期已存在领取记录（{existing.status}）",
            detail={"idempotency_key": key},
        )

    record = Claim(
        faucet_id=faucet.id,
        wallet_id=wallet.id,
        project_id=project.id,
        chain_id=chain.id,
        mode="AUTO",
        status="ATTEMPTED",
        idempotency_key=key,
        evidence={"triggered_by": triggered_by, "gate_detail": decision.detail},
    )
    session.add(record)
    await session.flush()          # 先把幂等键占住。UNIQUE 冲突会在这里抛出。

    # ── 没有签名能力就到此为止 ──
    if not signing_available():
        record.status = "SKIPPED_NO_KEY"
        record.error = "没有可用的签名密钥"
        await session.flush()
        return ExecutionResult(ok=False, status="SKIPPED_NO_KEY",
                               error=record.error)

    try:
        account, signer = load_signing_key(expected_address=wallet.address)
    except WalletKeyError as exc:
        # WalletKeyError 的消息经过审查，不含密钥内容
        record.status = "SKIPPED_NO_KEY"
        record.error = str(exc)
        await session.flush()
        return ExecutionResult(ok=False, status="SKIPPED_NO_KEY", error=str(exc))

    req = ClaimRequest(
        faucet_id=faucet.id,
        faucet_url=faucet.url,
        wallet_address=wallet.address,
        chain_key=chain.key,
        idempotency_key=key,
    )

    # ── 构造交易 ──
    try:
        from app.chains.registry import make_adapter

        chain_adapter = make_adapter(chain)
        if not chain_adapter.configured:
            raise RuntimeError(f"RPC 未配置（{chain.rpc_url_env}）")

        async with chain_adapter:
            built = await adapters.build_transaction(req, chain_adapter)
            if not built.ok:
                raise RuntimeError(built.error or "适配器无法构造交易")

            to_addr = built.to_address
            data = built.data
            value = int(built.value or 0)

            gas_est = await chain_adapter.estimate_gas(to_addr, data, sender=wallet.address, value=value)
            if not gas_est.ok or gas_est.value is None:
                raise RuntimeError(f"gas 估算失败: {gas_est.error}")
            gas_limit = min(
                int(Decimal(int(gas_est.value)) * DEFAULT_GAS_LIMIT_MULTIPLIER),
                HARD_GAS_LIMIT,
            )

            gp = await chain_adapter.gas_price()
            if not gp.ok or gp.value is None:
                raise RuntimeError(f"gas 价格获取失败: {gp.error}")
            gas_price = int(gp.value)

            nonce_res = await chain_adapter.transaction_count_pending(wallet.address)
            if not nonce_res.ok or nonce_res.value is None:
                raise RuntimeError(f"nonce 获取失败: {nonce_res.error}")
            nonce = int(nonce_res.value)

            # ── 成本闸 ──
            price = await get_price_provider().price_usd(chain.native_symbol)
            est_usd = compute_usd(gas_limit, gas_price, price)
            if not chain.is_testnet:
                if settings.auto_claim_max_usd <= 0:
                    raise RuntimeError(
                        "主网链且 AUTO_CLAIM_MAX_USD=0 —— 拒绝支出真实资金"
                    )
                if est_usd is not None and float(est_usd) > settings.auto_claim_max_usd:
                    raise RuntimeError(
                        f"预估成本 ${float(est_usd):.4f} 超过上限 ${settings.auto_claim_max_usd}"
                    )

            balance = await chain_adapter.native_balance(wallet.address)
            if balance.ok and balance.value is not None:
                needed = gas_limit * gas_price
                if int(balance.value) < needed:
                    raise RuntimeError(
                        f"原生币不足付 gas：需要 {needed / 1e18:.8f} {chain.native_symbol}，"
                        f"当前 {int(balance.value) / 1e18:.8f}"
                    )

            tx = {
                "to": to_addr,
                "value": value,
                "data": data,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "chainId": chain.chain_id,
            }

            # ── 签名（离线，不联网） ──
            signed = account.sign_transaction(tx)
            raw_tx = signed.raw_transaction
            tx_hash = signed.hash.hex()
            if not tx_hash.startswith("0x"):
                tx_hash = "0x" + tx_hash

            detail = {
                "to": to_addr,
                "gas_limit": gas_limit,
                "gas_price_wei": gas_price,
                "nonce": nonce,
                "estimated_usd": str(est_usd) if est_usd is not None else None,
                "tx_hash": tx_hash,
                "signer": signer.address,
                "signer_fingerprint": signer.key_fingerprint,
                "calldata_preview": data[:80],
            }

            # ── DRY RUN：到这里为止，签名结果直接丢弃 ──
            if settings.auto_claim_dry_run:
                record.status = "DRY_RUN"
                record.error = None
                record.tx_hash = None
                record.evidence = {**record.evidence, **detail, "dry_run": True}
                await session.flush()
                log.info("claim_dry_run", extra={"faucet": faucet.id, **detail})
                return ExecutionResult(
                    ok=False, status="DRY_RUN",
                    error="dry-run：已构造并签名，但未广播（AUTO_CLAIM_DRY_RUN=true）",
                    detail=detail, signed_but_not_sent=True,
                )

            # ── 广播 ──
            sent = await chain_adapter.send_raw_transaction(raw_tx)
            del raw_tx, signed              # 尽快丢掉原始交易
            if not sent.ok:
                raise RuntimeError(f"广播失败: {sent.error}")

            record.tx_hash = sent.value or tx_hash
            record.status = "SUBMITTED"
            record.evidence = {**record.evidence, **detail}
            if faucet.cooldown_seconds:
                record.next_available_at = datetime.now(UTC) + timedelta(
                    seconds=faucet.cooldown_seconds
                )

            session.add(
                Transaction(
                    chain_id=chain.id,
                    wallet_id=wallet.id,
                    project_id=project.id,
                    tx_hash=record.tx_hash,
                    direction="OUT",
                    status="PENDING",
                    occurred_at=datetime.now(UTC),
                    raw={"source": "autoclaim", "faucet_id": faucet.id},
                )
            )
            await session.flush()

            # ── 等确认 ──
            receipt = await chain_adapter.wait_for_receipt(record.tx_hash, timeout=120)
            if receipt is not None:
                ok = int(receipt.get("status", "0x0"), 16) == 1
                record.status = "SUCCESS" if ok else "FAILED"
                record.completed_at = datetime.now(UTC)
                if not ok:
                    record.error = "交易已上链但执行失败（status=0）"
                detail["block_number"] = int(receipt.get("blockNumber", "0x0"), 16)
            else:
                record.status = "SUBMITTED"
                detail["note"] = "120 秒内未确认，交给 costs 任务后续跟踪"

        log.info("claim_executed", extra={"faucet": faucet.id, "status": record.status, **detail})
        return ExecutionResult(
            ok=record.status in ("SUCCESS", "SUBMITTED"),
            status=record.status,
            tx_hash=record.tx_hash,
            error=record.error,
            detail=detail,
        )

    except Exception as exc:  # noqa: BLE001 - 必须记录并返回，不能抛出
        record.status = "FAILED"
        record.error = f"{type(exc).__name__}: {exc}"
        record.completed_at = datetime.now(UTC)
        try:
            await session.flush()
        except Exception:  # noqa: BLE001
            log.exception("claim_record_write_failed")
        log.warning("claim_failed", extra={"faucet": faucet.id, "error": record.error})
        return ExecutionResult(ok=False, status="FAILED", error=record.error)

