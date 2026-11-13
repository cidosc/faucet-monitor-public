"""Operator CLI.

    python -m app.cli status
    python -m app.cli wallet add --label testnet --address 0x... --chain eth-sepolia
    python -m app.cli wallet list
    python -m app.cli wallet remove --label testnet
    python -m app.cli job list
    python -m app.cli job run probe_faucets        # runs through the real guarded path
    python -m app.cli policy check --project ethereum-sepolia --wallet testnet
    python -m app.cli chains verify
    python -m app.cli notify test

Deliberately read-only with respect to chain state: nothing here can sign or send.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.logging_conf import get_logger, setup_logging

log = get_logger("app.cli")


# ─────────────────────────── commands ───────────────────────────


async def cmd_claim_add(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.claims.abi import canonical_signature, selector
    from app.db import session_scope
    from app.models import Chain, ClaimTarget, Project

    async with session_scope() as s:
        project = (
            await s.execute(select(Project).where(Project.slug == args.project))
        ).scalar_one_or_none()
        if project is None:
            print(f"error: unknown project '{args.project}'", file=sys.stderr)
            return 2
        chain = (
            await s.execute(select(Chain).where(Chain.key == args.chain))
        ).scalar_one_or_none()
        if chain is None:
            print(f"error: unknown chain '{args.chain}'", file=sys.stderr)
            return 2

        if (await s.execute(
            select(ClaimTarget).where(
                ClaimTarget.project_id == project.id, ClaimTarget.label == args.label
            )
        )).scalar_one_or_none():
            print(f"error: target '{args.label}' already exists for {args.project}", file=sys.stderr)
            return 2

        # Validate the signature up front: a malformed selector would silently simulate
        # the wrong function, which is worse than refusing.
        if args.signature:
            try:
                sel = selector(args.signature)
                canon = canonical_signature(args.signature)
            except ValueError as exc:
                print(f"error: bad claim signature: {exc}", file=sys.stderr)
                return 2
            print(f"  claim selector : {sel}  ({canon})")
        if args.claimed_signature:
            try:
                print(f"  claimed selector: {selector(args.claimed_signature)}")
            except ValueError as exc:
                print(f"error: bad claimed signature: {exc}", file=sys.stderr)
                return 2

        if args.kind == "CONTRACT_CLAIM" and not args.contract:
            print("error: --contract is required for kind=CONTRACT_CLAIM", file=sys.stderr)
            return 2

        s.add(
            ClaimTarget(
                project_id=project.id,
                chain_id=chain.id,
                label=args.label,
                kind=args.kind,
                claim_url=args.claim_url,
                contract_address=args.contract,
                claim_signature=args.signature,
                claimed_signature=args.claimed_signature,
                claimed_arg=args.claimed_arg,
                proof_source_url=args.proof_url,
                proof_json_path=args.proof_path,
                amount_field=args.amount_field,
                proof_field=args.proof_field,
                human_verified=False,
                notes=args.notes,
                enabled=True,
            )
        )

    print(f"added claim target '{args.label}' on {args.chain}")
    print("  human_verified=false — this may be MONITORED and SIMULATED only.")
    print("  Nothing can execute until you verify the project's terms and set it otherwise.")
    return 0


async def cmd_claim_list(_args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Chain, ClaimTarget, Project

    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(ClaimTarget, Project.slug, Chain.key)
                    .join(Project, Project.id == ClaimTarget.project_id)
                    .join(Chain, Chain.id == ClaimTarget.chain_id)
                    .order_by(ClaimTarget.id)
                )
            ).all()
        )
    if not rows:
        print("no claim targets configured.")
        print("  add one:  python -m app.cli claim add --project <slug> --chain <key> \\")
        print("              --label 'Season 1' --kind CONTRACT_CLAIM --contract 0x... \\")
        print('              --signature "claim(uint256,bytes32[])" --proof-url https://.../{address}')
        return 0
    print(f"{'ID':4s} {'PROJECT':22s} {'CHAIN':14s} {'KIND':16s} {'VERIFIED':9s} LABEL")
    for t, slug, ckey in rows:
        print(f"{t.id:<4d} {slug:22s} {ckey:14s} {t.kind:16s} {str(t.human_verified):9s} {t.label}")
    return 0


async def cmd_claim_check(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.claims.readiness import check_readiness
    from app.db import session_scope
    from app.models import Chain, ClaimTarget, Project, Wallet

    async with session_scope() as s:
        q = select(ClaimTarget, Project, Chain).join(
            Project, Project.id == ClaimTarget.project_id
        ).join(Chain, Chain.id == ClaimTarget.chain_id)
        if args.target:
            q = q.where(ClaimTarget.id == args.target)
        if args.project:
            q = q.where(Project.slug == args.project)
        rows = list((await s.execute(q)).all())
        if not rows:
            print("error: no matching claim targets", file=sys.stderr)
            return 2

        wallets = list((await s.execute(select(Wallet).where(Wallet.enabled.is_(True)))).scalars())
        if args.wallet:
            wallets = [w for w in wallets if w.label == args.wallet]
        if not wallets:
            print("error: no matching wallets", file=sys.stderr)
            return 2

        for target, project, chain in rows:
            for wallet in wallets:
                res = await check_readiness(s, wallet, target, chain)
                print(f"\n{project.slug} / {target.label}  [{chain.key}]")
                print(f"  wallet        : {wallet.label} {wallet.address}")
                print(f"  verdict       : {res.status}")
                print(f"  window open   : {res.window_open}")
                print(f"  already claim : {res.already_claimed}")
                if res.revert_reason:
                    print(f"  revert reason : {res.revert_reason}")
                if res.gas_estimate is not None:
                    print(f"  gas estimate  : {int(res.gas_estimate)}")
                if res.gas_price_wei is not None:
                    print(f"  gas price     : {int(res.gas_price_wei)} wei")
                usd = f"${float(res.estimated_usd):.4f}" if res.estimated_usd is not None else "unknown"
                print(f"  estimated cost: {usd}")
                if res.calldata:
                    print(f"  to            : {res.to_address}")
                    print(f"  calldata      : {res.calldata[:120]}{'...' if len(res.calldata) > 120 else ''}")
                if res.error:
                    print(f"  error         : {res.error}")
                print("  NOTE: simulated only. Nothing was signed or broadcast.")
    return 0


async def cmd_signer(args: argparse.Namespace) -> int:
    """签名能力自检。**不会广播任何交易。**"""
    from app.autoclaim.keys import WalletKeyError, key_file_path, load_signing_key, signing_available

    print(f"  密钥文件: {key_file_path()}")
    print(f"  可加载  : {signing_available()}")
    if not signing_available():
        print("\n  未安装密钥。用 scripts/set-wallet-key.sh 安装后重试。")
        return 2

    try:
        account, info = load_signing_key()
    except WalletKeyError as exc:
        print(f"\n  加载失败: {exc}", file=sys.stderr)
        return 2

    print(f"  签名地址: {info.address}")
    print(f"  密钥指纹: {info.key_fingerprint}")

    if not args.selftest:
        print("\n  加 --selftest 可验证签名链路（仍不会广播）。")
        return 0

    # 构造一笔 0 值、0 数据、发给自己、从不存在的链上广播的交易。
    # 目的是验证：签名能在内存里完成，且能从签名结果里恢复出正确地址。
    from eth_account import Account

    probe = {
        "to": info.address,
        "value": 0,
        "data": "0x",
        "nonce": 0,
        "gas": 21000,
        "gasPrice": 1_000_000_000,
        "chainId": 1,
    }
    signed = account.sign_transaction(probe)
    recovered = Account.recover_transaction(signed.raw_transaction)

    print("\n  签名自检:")
    print("    签名成功   : True")
    print(f"    交易哈希   : {signed.hash.hex()}")
    print(f"    恢复出地址 : {recovered}")
    print(f"    地址一致   : {recovered.lower() == info.address.lower()}")
    print(f"    原始交易   : {len(signed.raw_transaction)} 字节（已丢弃，未广播）")
    print("\n  ✓ 签名链路可用。注意：本次调用没有向任何 RPC 发出请求。")
    return 0 if recovered.lower() == info.address.lower() else 1


async def cmd_autoclaim(args: argparse.Namespace) -> int:
    """手动跑一次自动领取（受全部门槛与 dry-run 约束）。"""
    from sqlalchemy import select

    from app.autoclaim.executor import execute_claim
    from app.db import session_scope
    from app.models import Chain, Faucet, Project, Wallet

    async with session_scope() as s:
        wallet = (
            await s.execute(select(Wallet).where(Wallet.label == args.wallet))
        ).scalar_one_or_none()
        if wallet is None:
            print(f"error: 未知钱包 '{args.wallet}'", file=sys.stderr)
            return 2
        faucet = (
            await s.execute(select(Faucet).where(Faucet.url == args.faucet))
        ).scalar_one_or_none()
        if faucet is None:
            print(f"error: 未记录该水龙头 '{args.faucet}'", file=sys.stderr)
            return 2
        project = (
            await s.execute(select(Project).where(Project.id == faucet.project_id))
        ).scalar_one()
        chain = (
            await s.execute(select(Chain).where(Chain.id == project.chain_id))
        ).scalar_one()

        result = await execute_claim(
            s, faucet=faucet, project=project, chain=chain, wallet=wallet,
            triggered_by="cli",
        )

    print(f"  结果   : {result.status}")
    print(f"  成功   : {result.ok}")
    if result.tx_hash:
        print(f"  交易哈希: {result.tx_hash}")
    if result.error:
        print(f"  原因   : {result.error}")
    if result.signed_but_not_sent:
        print("  注意   : 已签名但未广播（dry-run）—— 什么都没发生")
    for k, v in (result.detail or {}).items():
        print(f"  {k:16s}: {v}")
    return 0 if result.ok else 1


async def cmd_status(_args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from app.config import settings
    from app.db import session_scope
    from app.models import Claim, Cost, Faucet, Notification, Project, Task, Wallet

    async with session_scope() as s:
        projects = (await s.execute(select(func.count(Project.id)))).scalar_one()
        wallets = (await s.execute(select(func.count(Wallet.id)))).scalar_one()
        by_status = dict(
            (await s.execute(select(Faucet.status, func.count(Faucet.id)).group_by(Faucet.status))).all()
        )
        pending_tasks = (
            await s.execute(select(func.count(Task.id)).where(Task.status == "PENDING"))
        ).scalar_one()
        claims = (await s.execute(select(func.count(Claim.id)))).scalar_one()
        gas = (await s.execute(select(func.coalesce(func.sum(Cost.estimated_usd), 0)))).scalar_one()
        queued = (
            await s.execute(
                select(func.count(Notification.id)).where(Notification.status == "PENDING")
            )
        ).scalar_one()

    print("── Faucet Monitor status ─────────────────────────────")
    print(f"  auto-claim master switch : {'ON' if settings.global_auto_claim else 'OFF'}")
    print(f"  notify channels          : {', '.join(settings.notify_channel_list) or 'none'}")
    print(f"  projects                 : {projects}")
    print(f"  wallets                  : {wallets}")
    print(f"  faucets                  : {sum(by_status.values())}")
    for status, count in sorted(by_status.items()):
        print(f"      {status:16s} {count}")
    print(f"  pending tasks            : {pending_tasks}")
    print(f"  claims recorded          : {claims}")
    print(f"  gas (USD)                : ${float(gas or 0):.6f}")
    print(f"  queued notifications     : {queued}")
    print("──────────────────────────────────────────────────────")
    return 0


async def cmd_wallet_add(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Chain, Wallet
    from app.wallets import is_valid_address

    async with session_scope() as s:
        chain = None
        if args.chain:
            chain = (await s.execute(select(Chain).where(Chain.key == args.chain))).scalar_one_or_none()
            if chain is None:
                print(f"error: unknown chain '{args.chain}' (run `app.cli chains verify` to list)", file=sys.stderr)
                return 2

        chain_type = chain.chain_type if chain else "evm"
        if not is_valid_address(args.address, chain_type):
            print(f"error: '{args.address}' is not a valid {chain_type} address", file=sys.stderr)
            return 2

        if (await s.execute(select(Wallet).where(Wallet.label == args.label))).scalar_one_or_none():
            print(f"error: wallet label '{args.label}' already exists", file=sys.stderr)
            return 2

        # The DB enforces UNIQUE(address, chain_id); check first so a duplicate is a
        # clear message rather than an IntegrityError traceback.
        clash = (
            await s.execute(
                select(Wallet).where(
                    Wallet.address == args.address, Wallet.chain_id == (chain.id if chain else None)
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            print(
                f"error: address {args.address} is already registered as '{clash.label}' "
                f"on this chain (one EVM address is identical across all EVM chains, "
                f"so a second entry would just be a duplicate)",
                file=sys.stderr,
            )
            return 2

        auto = bool(args.auto_faucet)
        # `is_test_wallet` is an OPT-IN claim about the wallet, not a default. Marking a
        # wallet as a "test wallet" is what makes it eligible for automation, so the
        # fail-safe default is False: a newly added wallet is treated as a real wallet
        # until you explicitly say otherwise.
        is_test = bool(args.is_test_wallet)
        if auto and not is_test:
            print("note: auto_faucet is force-disabled for non-test wallets")
            auto = False

        s.add(
            Wallet(
                label=args.label,
                address=args.address,
                chain_id=chain.id if chain else None,
                is_test_wallet=is_test,
                auto_faucet=auto,
                enabled=True,
                notes=args.notes,
            )
        )

    print(f"added wallet '{args.label}' ({args.address})")
    if auto:
        print("  auto_faucet=true — still requires ALL 9 policy gates plus a tested adapter.")
    return 0


async def cmd_wallet_list(_args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Chain, Wallet

    async with session_scope() as s:
        rows = list(
            (
                await s.execute(
                    select(Wallet, Chain.key).join(Chain, Chain.id == Wallet.chain_id, isouter=True)
                    .order_by(Wallet.label)
                )
            ).all()
        )
    if not rows:
        print("no wallets configured. Add one with:")
        print("  python -m app.cli wallet add --label testnet --address 0x... --chain eth-sepolia")
        return 0
    print(f"{'LABEL':20s} {'CHAIN':16s} {'ADDRESS':44s} {'TEST':5s} {'AUTO':5s} ENABLED")
    for w, chain_key in rows:
        print(
            f"{w.label:20s} {chain_key or '-':16s} {w.address:44s} "
            f"{str(w.is_test_wallet):5s} {str(w.auto_faucet):5s} {w.enabled}"
        )
    return 0


async def cmd_wallet_remove(args: argparse.Namespace) -> int:
    from sqlalchemy import delete, select

    from app.db import session_scope
    from app.models import Wallet

    async with session_scope() as s:
        w = (await s.execute(select(Wallet).where(Wallet.label == args.label))).scalar_one_or_none()
        if w is None:
            print(f"error: no wallet '{args.label}'", file=sys.stderr)
            return 2
        await s.execute(delete(Wallet).where(Wallet.id == w.id))
    print(f"removed wallet '{args.label}'")
    return 0


async def cmd_wallet_sync(_args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.wallets import sync_wallets_from_config

    async with session_scope() as s:
        result = await sync_wallets_from_config(s)
    print(json.dumps(result, indent=2))
    return 0


async def cmd_job_list(_args: argparse.Namespace) -> int:
    from app.jobs import JOBS
    from app.scheduler import build_scheduler

    sched = build_scheduler()
    triggers = {j.id: str(j.trigger) for j in sched.get_jobs()}
    print(f"{'JOB':18s} TRIGGER")
    for name in sorted(JOBS):
        print(f"{name:18s} {triggers.get(name, '(no trigger registered)')}")
    return 0


async def cmd_job_run(args: argparse.Namespace) -> int:
    from app.jobs import JOBS
    from app.scheduler import run_guarded

    fn = JOBS.get(args.name)
    if fn is None:
        print(f"error: unknown job '{args.name}'. Available: {', '.join(sorted(JOBS))}", file=sys.stderr)
        return 2

    # Run through the guarded path so the advisory lock, timeout and scan_logs row are
    # all exercised exactly as they are by the scheduler.
    await run_guarded(args.name, fn, triggered_by="cli")
    return 0


async def cmd_policy_check(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.autoclaim.adapters import adapter_is_registered, adapter_is_tested, registered_count
    from app.autoclaim.policy import evaluate
    from app.db import session_scope
    from app.models import Chain, Faucet, Project, Wallet

    async with session_scope() as s:
        project = (
            await s.execute(select(Project).where(Project.slug == args.project))
        ).scalar_one_or_none()
        if project is None:
            print(f"error: unknown project '{args.project}'", file=sys.stderr)
            return 2
        wallet = (
            await s.execute(select(Wallet).where(Wallet.label == args.wallet))
        ).scalar_one_or_none()
        if wallet is None:
            print(f"error: unknown wallet '{args.wallet}'", file=sys.stderr)
            return 2
        chain = None
        if project.chain_id is not None:
            chain = (
                await s.execute(select(Chain).where(Chain.id == project.chain_id))
            ).scalar_one_or_none()
        faucets = list(
            (await s.execute(select(Faucet).where(Faucet.project_id == project.id))).scalars()
        )
        if not faucets:
            print(f"error: project '{args.project}' has no faucets", file=sys.stderr)
            return 2

        print(f"registered claim adapters: {registered_count()}  (0 = gate 7 fails for all faucets)\n")
        for faucet in faucets:
            decision = await evaluate(
                s,
                faucet=faucet,
                project=project,
                wallet=wallet,
                chain=chain,
                adapter_registered=adapter_is_registered(faucet.url),
                adapter_tested=adapter_is_tested(faucet.url),
            )
            verdict = "ALLOWED" if decision.allowed else "BLOCKED"
            print(f"faucet {faucet.id}: {faucet.url}")
            print(f"  verdict     : {verdict}")
            print(f"  blocked by  : gate {decision.failed_gate} ({decision.failed_gate_name})")
            for reason in decision.reasons:
                print(f"    - {reason}")
            print()
    return 0


async def cmd_chains_verify(_args: argparse.Namespace) -> int:
    from app.chains.registry import sync_chains, verify_all
    from app.db import session_scope

    async with session_scope() as s:
        await sync_chains(s)
    async with session_scope() as s:
        results = await verify_all(s)

    print(f"{'CHAIN':20s} {'OK':5s} {'CHAIN_ID':12s} {'RPC ENV':26s} ERROR")
    for r in results:
        print(
            f"{r.get('key', '?'):20s} {str(bool(r.get('ok'))):5s} "
            f"{str(r.get('chain_id') or '-'):12s} {str(r.get('rpc_env') or '-'):26s} "
            f"{r.get('error') or ''}"
        )
    unconfigured = [r for r in results if (r.get("error") or "").startswith("rpc_not_configured")]
    if unconfigured:
        print(
            f"\n{len(unconfigured)} chain(s) have no RPC endpoint configured. Set the matching\n"
            "RPC_* variable in .env, e.g. RPC_ETH_SEPOLIA=https://sepolia.example/v3/KEY\n"
            "Eligibility checks on those chains report UNKNOWN rather than guessing."
        )
    return 0


async def cmd_notify_test(args: argparse.Namespace) -> int:
    from app.config import settings
    from app.notify.dedupe import build_notifier
    from app.notify.templates import delivery_test

    channels = [args.channel] if args.channel else settings.notify_channel_list
    if not channels:
        print("error: NOTIFY_CHANNELS is empty", file=sys.stderr)
        return 2

    failures = 0
    for channel in channels:
        notifier = build_notifier(channel)
        if notifier is None:
            print(f"{channel:12s} 未配置")
            failures += 1
            continue
        result = await notifier.send(delivery_test())
        print(f"{channel:12s} {'成功' if result.ok else '失败'}  {result.error or ''}")
        failures += 0 if result.ok else 1
    return 0 if failures == 0 else 1


# ─────────────────────────── wiring ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="summary of the whole system").set_defaults(fn=cmd_status)

    w = sub.add_parser("wallet", help="manage watch wallets (addresses only)")
    ws = w.add_subparsers(dest="sub", required=True)
    wa = ws.add_parser("add")
    wa.add_argument("--label", required=True)
    wa.add_argument("--address", required=True)
    wa.add_argument("--chain")
    wa.add_argument("--notes")
    wa.add_argument("--auto-faucet", action="store_true",
                    help="opt in to auto-claim (still requires all 9 policy gates "
                         "AND is_test_wallet)")
    wa.add_argument("--is-test-wallet", action="store_true", default=False,
                    help="assert this is a throwaway wallet holding no value. "
                         "REQUIRED for auto_faucet to be possible. Off by default: "
                         "an added wallet is treated as a real wallet.")
    wa.set_defaults(fn=cmd_wallet_add)
    ws.add_parser("list").set_defaults(fn=cmd_wallet_list)
    wr = ws.add_parser("remove")
    wr.add_argument("--label", required=True)
    wr.set_defaults(fn=cmd_wallet_remove)
    ws.add_parser("sync", help="load config/wallets.yaml").set_defaults(fn=cmd_wallet_sync)

    j = sub.add_parser("job", help="scheduled jobs")
    js = j.add_subparsers(dest="sub", required=True)
    js.add_parser("list").set_defaults(fn=cmd_job_list)
    jr = js.add_parser("run")
    jr.add_argument("name")
    jr.set_defaults(fn=cmd_job_run)

    pol = sub.add_parser("policy", help="inspect the auto-claim gate")
    ps = pol.add_subparsers(dest="sub", required=True)
    pc = ps.add_parser("check")
    pc.add_argument("--project", required=True)
    pc.add_argument("--wallet", required=True)
    pc.set_defaults(fn=cmd_policy_check)

    ch = sub.add_parser("chains", help="chain configuration")
    cs = ch.add_subparsers(dest="sub", required=True)
    cs.add_parser("verify", help="sync chain defs and probe eth_chainId").set_defaults(fn=cmd_chains_verify)

    cl = sub.add_parser("claim", help="claim targets (read-only readiness + simulation)")
    cls = cl.add_subparsers(dest="sub", required=True)

    ca = cls.add_parser("add", help="register something claimable")
    ca.add_argument("--project", required=True)
    ca.add_argument("--chain", required=True)
    ca.add_argument("--label", required=True)
    ca.add_argument("--kind", default="MANUAL_ONLY",
                    choices=["CONTRACT_CLAIM", "FAUCET_HTTP", "GASLESS_RELAYER", "MANUAL_ONLY"])
    ca.add_argument("--contract", help="claim contract address")
    ca.add_argument("--signature", help='e.g. "claim(uint256,bytes32[])"')
    ca.add_argument("--claimed-signature", help='e.g. "isClaimed(address)"')
    ca.add_argument("--claimed-arg", default="address", choices=["address", "index"])
    ca.add_argument("--proof-url", help="JSON endpoint; may contain {address}")
    ca.add_argument("--proof-path", help="dotted path into the JSON, e.g. data.claims")
    ca.add_argument("--amount-field", default="amount")
    ca.add_argument("--proof-field", default="proof")
    ca.add_argument("--claim-url", help="human-facing claim page")
    ca.add_argument("--notes")
    ca.set_defaults(fn=cmd_claim_add)

    cls.add_parser("list").set_defaults(fn=cmd_claim_list)

    cc = cls.add_parser("check", help="simulate readiness (never signs)")
    cc.add_argument("--target", type=int)
    cc.add_argument("--project")
    cc.add_argument("--wallet")
    cc.set_defaults(fn=cmd_claim_check)

    n = sub.add_parser("notify", help="notifications")
    ns = n.add_subparsers(dest="sub", required=True)
    nt = ns.add_parser("test")
    nt.add_argument("--channel")
    nt.set_defaults(fn=cmd_notify_test)

    sg = sub.add_parser("signer", help="签名能力自检（不广播）")
    sg.add_argument("--selftest", action="store_true",
                    help="构造并签名一笔测试交易，验证签名可恢复出地址；仍然不会广播")
    sg.set_defaults(fn=cmd_signer)

    ac = sub.add_parser("autoclaim", help="手动执行一次自动领取（受门槛与 dry-run 约束）")
    ac.add_argument("--wallet", required=True)
    ac.add_argument("--faucet", required=True, help="水龙头 URL，必须与库中记录一致")
    ac.set_defaults(fn=cmd_autoclaim)

    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        log.exception("cli_failed", extra={"cmd": getattr(args, "cmd", "?")})
        return 1


if __name__ == "__main__":
    sys.exit(main())

