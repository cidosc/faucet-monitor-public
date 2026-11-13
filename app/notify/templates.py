"""所有用户可见的通知文案集中在这里（中文）。

为什么集中管理：文案散落在 job 和事件代码里时，改一次措辞要翻五个文件，而且很容易
漏掉某一条 —— 这正是本次审计发现的问题（9 个模板全是英文，分散在 5 个文件）。

约定：
  * 标题一行，带 emoji，一眼能分清类型
  * 正文用「标签: 值」对齐排列，方便在飞书里扫读
  * **不夸大、不承诺**。模拟成功就写模拟成功，不说「已可以领取」；
    gas 高就明说建议等待，不诱导花钱
  * 涉及资金的操作一律明确写出「这是通知，不是操作」
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.notify.base import Message

# ─────────────────────────── 通用小工具 ───────────────────────────


def fmt_amount(value: Any, decimals: int = 6) -> str:
    if value is None:
        return "未知"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == 0:
        return "0"
    if abs(f) < 10 ** (-decimals):
        return f"{f:.12f}".rstrip("0").rstrip(".")
    return f"{f:,.{decimals}f}".rstrip("0").rstrip(".")


def fmt_usd(value: Any) -> str:
    if value is None:
        return "未知（缺少价格源）"
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return str(value)


def fmt_gwei(wei: Any) -> str:
    if wei is None:
        return "未知"
    try:
        return f"{float(wei) / 1e9:,.4f} gwei"
    except (TypeError, ValueError):
        return str(wei)


def fmt_time(value: Any) -> str:
    if isinstance(value, datetime):
        v = value if value.tzinfo else value.replace(tzinfo=UTC)
        return v.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return str(value) if value else "未知"


def short(addr: str | None, keep: int = 10) -> str:
    if not addr:
        return "未知"
    return addr if len(addr) <= keep * 2 else f"{addr[:keep]}…{addr[-6:]}"


def chain_label(name: str, is_testnet: bool | None) -> str:
    if is_testnet is None:
        return name
    return f"{name}（{'测试网' if is_testnet else '主网'}）"


def requirement_hint(faucet: Any) -> str:
    bits: list[str] = []
    if getattr(faucet, "requires_wallet_address", None):
        bits.append("需要钱包地址")
    if getattr(faucet, "requires_login", None):
        bits.append(f"需要登录（{getattr(faucet, 'login_provider', None) or '未知方式'}）")
    if getattr(faucet, "requires_captcha", None):
        bits.append(f"需要人机验证（{getattr(faucet, 'captcha_type', None) or '未知类型'}）")
    cd = getattr(faucet, "cooldown_seconds", None)
    if cd:
        bits.append(f"冷却约 {cd // 3600} 小时" if cd >= 3600 else f"冷却约 {cd // 60} 分钟")
    return "、".join(bits) if bits else "未检测到明显门槛"


def automation_hint(policy: str | None) -> str:
    if policy == "ALLOWED":
        return "已人工核实允许自动化"
    if policy == "FORBIDDEN":
        return "项目规则明确禁止 —— 仅监控，不执行"
    return "未验证 —— 只监控，绝不自动执行"


# ── 诊断原因翻译 ──
# classifier.py / readiness.py 生成的原因字符串刻意保持英文：它们同时写进 scan_logs
# 和 evidence，需要可 grep、可对比。所以只在展示层翻译。
# 未命中任何规则时原样输出，而不是编造一个听起来合理的说法。
_REASON_RULES: list[tuple[str, str]] = [
    (r"disallowed_by_robots_txt", "目标站点 robots.txt 禁止抓取"),
    (r"no_response[: ]*(.*)", "无响应（{1}）"),
    (r"url_gone \(HTTP (\d+)\)", "页面已不存在（HTTP {1}）"),
    (r"server_error \(HTTP (\d+)\)", "服务器错误（HTTP {1}）"),
    (r"cloudflare_challenge_detected", "触发 Cloudflare 人机验证，未尝试绕过"),
    (r"captcha_detected \(([^)]*)\)", "需要验证码（{1}），未尝试绕过"),
    (r"access_control \(HTTP (\d+)", "访问被拒绝（HTTP {1}）"),
    (r"maintenance_or_paused_notice", "页面显示维护或暂停"),
    (r"login_required \(([^)]*)\)", "需要登录（{1}）"),
    (r"faucet_reports_empty", "水龙头已空"),
    (r"cooldown_indicated[,，]?\s*~?(\d+)s", "处于冷却中（约 {1} 秒）"),
    (r"cooldown_indicated", "处于冷却中"),
    (r"wallet_address_form_present", "页面存在钱包地址表单，未检测到阻碍"),
    (r"reachable_but_no_wallet_form_found", "页面可访问，但未找到钱包地址表单"),
    (r"unhandled_http_status \(([^)]*)\)", "未预期的 HTTP 状态（{1}）"),
    (r"downgraded_to_UNKNOWN", "置信度不足，已归为未知，不做猜测"),
    (r"probe_exception[: ]*(.*)", "探测异常（{1}）"),
    (r"window has not started", "领取窗口尚未开始"),
    (r"window has ended", "领取窗口已结束"),
    (r"kind=(\w+) has no callable claim signature", "该目标（{1}）没有可调用的领取函数，仅监控"),
    (r"faucet is AVAILABLE and no blocker detected", "水龙头当前可用，且未检测到阻碍"),
    (r"faucet reports cooldown", "水龙头报告处于冷却中"),
    (r"faucet is empty", "水龙头已空"),
    (r"faucet offline", "水龙头离线"),
    (r"faucet status is (\w+)", "水龙头状态为 {1}，无法判定就绪与否"),
    (r"human verification required \(([^)]*)\)", "需要人机验证（{1}），无法自动化"),
    (r"login required \(([^)]*)\)", "需要登录（{1}）"),
    (r"(\d+) requirement\(s\) failed on ([\w-]+)", "有 {1} 项条件未满足（链 {2}）"),
    (r"no probed faucet matches claim_url", "尚未探测过该地址；请先把它加入 config/sources.yaml"),
    (r"rpc_not_configured \(([^)]*)\)", "RPC 未配置（{1}），无法判定"),
    (r"already claimed", "链上记录显示已领取"),
    (r"contract_address_missing", "未配置领取合约地址"),
    (r"proof_path_not_found", "证明接口返回的数据结构与配置不符"),
    (r"proof_http_(\d+)", "证明接口返回 HTTP {1}"),
]

_REASON_COMPILED = [(re.compile(p, re.IGNORECASE), zh) for p, zh in _REASON_RULES]


def reason_zh(reason: str | None) -> str:
    """把诊断原因翻成中文。未命中规则就原样返回 —— 不编造。"""
    if not reason:
        return "未知"
    text = str(reason).strip()
    for pattern, template in _REASON_COMPILED:
        m = pattern.search(text)
        if m:
            try:
                return template.format(*([m.group(0)] + list(m.groups())))
            except (IndexError, KeyError):
                return template
    return text


def reasons_zh(reasons: Any) -> str:
    """拼接多条原因（分类器一次会给出好几条）。"""
    if isinstance(reasons, str):
        reasons = [reasons]
    if not reasons:
        return "未知"
    out: list[str] = []
    for r in reasons:
        zh = reason_zh(str(r).split(" :: ")[0])
        if zh not in out:
            out.append(zh)
    return "；".join(out)


# ─────────────────────────── 水龙头类 ───────────────────────────


def new_faucet(
    *, project: str, chain: str, is_testnet: bool | None, faucet_url: str,
    requirement: str, policy: str | None,
) -> Message:
    return Message(
        title="🆕 发现新水龙头",
        body="\n".join([
            f"项目:     {project}",
            f"链:       {chain_label(chain, is_testnet)}",
            f"地址:     {faucet_url}",
            "状态:     待首次探测",
            "",
            f"领取条件: {requirement}",
            f"自动化:   {automation_hint(policy)}",
        ]),
        type="NEW_FAUCET",
    )


def faucet_available(
    *, project: str, chain: str, is_testnet: bool | None, faucet_url: str,
    previous: str | None, confidence: float, requirement: str, policy: str | None,
) -> Message:
    return Message(
        title="🟢 水龙头已恢复可用",
        body="\n".join([
            f"项目:     {project}",
            f"链:       {chain_label(chain, is_testnet)}",
            f"地址:     {faucet_url}",
            f"状态:     {previous or '未知'} → AVAILABLE（置信度 {confidence:.2f}）",
            "",
            f"领取条件: {requirement}",
            f"自动化:   {automation_hint(policy)}",
            "",
            "请自行打开页面领取；本系统不会代替你操作。",
        ]),
        type="FAUCET_AVAILABLE",
    )


def faucet_failed(
    *, project: str, faucet_url: str, status: str, reason: str,
    http_status: int | None, consecutive: int,
) -> Message:
    body = [
        f"项目:     {project}",
        f"地址:     {faucet_url}",
        f"状态:     {status}",
        "",
        f"原因:     {reason_zh(reason)}",
        f"HTTP:     {http_status if http_status is not None else '无响应'}",
        f"连续失败: {consecutive} 次",
    ]
    if status == "OFFLINE" and consecutive >= 3:
        body.append("")
        body.append("连续多次失败，该项目可能已下线或迁移，建议人工确认。")
    return Message(title="🔴 水龙头异常", body="\n".join(body), type="FAUCET_FAILED")


# ─────────────────────────── 领取类 ───────────────────────────


def claim_ready(
    *, project: str, target: str, chain: str, is_testnet: bool | None,
    wallet_label: str, wallet_address: str, gas_estimate: Any, gas_price_wei: Any,
    estimated_usd: Any, claim_url: str | None, to_address: str | None,
    calldata: str | None, gas_acceptable: bool | None, max_gwei: float, max_usd: float,
) -> Message:
    if is_testnet:
        verdict = "⛽ Gas 为测试网代币，无实际成本"
    elif gas_acceptable is True:
        verdict = f"✅ 当前 gas 低于你的阈值（{max_gwei:g} gwei / ${max_usd:g}），适合领取"
    elif gas_acceptable is False:
        verdict = f"⏸️ 当前 gas 高于你的阈值（{max_gwei:g} gwei / ${max_usd:g}），建议等降下来再领"
    else:
        verdict = "❓ 成本无法确定（缺少价格源），请自行判断"

    body = [
        f"项目:     {project}",
        f"目标:     {target}",
        f"链:       {chain_label(chain, is_testnet)}",
        f"钱包:     {wallet_label}  {wallet_address}",
        "",
        "模拟结果: eth_call 成功 —— 这笔交易可以走通",
        f"Gas 用量: {int(gas_estimate) if gas_estimate is not None else '未知'}",
        f"Gas 价格: {fmt_gwei(gas_price_wei)}",
        f"预估成本: {fmt_usd(estimated_usd)}",
        f"成本判断: {verdict}",
        "",
        "⚠️ 这是通知，不是操作。系统没有签名，也没有广播任何交易。",
    ]
    if claim_url:
        body += ["", f"领取页面: {claim_url}"]
    if to_address and calldata:
        body += [
            "",
            "如需手动执行，调用数据如下：",
            f"  to:   {to_address}",
            f"  data: {calldata}",
        ]
    title = "🟢 可以领取了" if is_testnet or gas_acceptable is not False else "🟡 可以领取，但 gas 偏高"
    return Message(title=title, body="\n".join(body), type="CLAIM_READY")


def claim_already(
    *, project: str, target: str, wallet_label: str, evidence: str,
) -> Message:
    return Message(
        title="✔️ 已经领取过了",
        body="\n".join([
            f"项目:   {project}",
            f"目标:   {target}",
            f"钱包:   {wallet_label}",
            "",
            "链上证据显示这笔已经领过，无需重复操作。",
            f"依据:   {reason_zh(evidence)}",
        ]),
        type="CLAIM_ALREADY_CLAIMED",
    )


def claim_not_available(
    *, project: str, target: str, wallet_label: str, status: str, reason: str,
) -> Message:
    zh = {
        "NOT_ELIGIBLE": "不符合资格",
        "WINDOW_NOT_OPEN": "领取窗口尚未开放",
        "WINDOW_CLOSED": "领取窗口已结束",
        "COOLDOWN": "处于冷却中",
        "MANUAL_REQUIRED": "需要人工操作",
        "UNSIMULATED": "无法自动模拟",
        "UNKNOWN": "状态未知",
    }.get(status, status)
    return Message(
        title=f"🔴 暂不可领取（{zh}）",
        body="\n".join([
            f"项目:   {project}",
            f"目标:   {target}",
            f"钱包:   {wallet_label}",
            "",
            f"状态:   {status}",
            f"原因:   {reason_zh(reason)}",
        ]),
        type=f"CLAIM_{status}",
    )


# ─────────────────────────── 到账类 ───────────────────────────


def incoming_transfer(
    *, wallet_label: str, wallet_address: str, chain: str, is_testnet: bool | None,
    asset: str, amount: Any, contract: str | None, from_address: str | None,
    tx_hash: str | None, block_number: int | None, occurred_at: Any, category: str,
) -> Message:
    body = [
        f"钱包:   {wallet_label}  {wallet_address}",
        f"链:     {chain_label(chain, is_testnet)}",
        "",
        f"资产:   {asset}（{category}）",
        f"数量:   {fmt_amount(amount)}",
    ]
    if contract:
        body.append(f"合约:   {contract}")
    body += [
        f"来自:   {from_address or '未知'}",
        f"交易:   {tx_hash or '未知'}",
        f"区块:   {block_number if block_number is not None else '未知'}",
    ]
    if occurred_at:
        body.append(f"时间:   {fmt_time(occurred_at)}")
    body += [
        "",
        "⚠️ 未主动索取的入账是空投最常见的形态，但也常被用作钓鱼诱饵。",
        "   交互前请先在区块浏览器核对代币来源，不要盲目授权或兑换。",
    ]
    return Message(
        title=f"🪂 收到新代币：{asset}",
        body="\n".join(body),
        type="INCOMING_TRANSFER",
    )


# ─────────────────────────── 汇总类 ───────────────────────────


def daily_summary(
    *, day: str, new_projects: int, faucet_status: dict[str, int], pending_tasks: int,
    wallets: int, claims_ok: int, claims_total: int, gas_today: Any, gas_total: Any,
    auto_claim_on: bool, channels: list[str], failures: list[tuple[str, str]],
) -> Message:
    status_line = "、".join(f"{k}={v}" for k, v in sorted(faucet_status.items())) or "暂无水龙头"
    lines = [
        f"日期:         {day}",
        "",
        f"新发现项目:   {new_projects}",
        f"水龙头状态:   {status_line}",
        f"待处理任务:   {pending_tasks}",
        f"钱包数量:     {wallets}",
        f"领取记录:     {claims_ok}/{claims_total} 成功",
        f"今日 Gas:     {fmt_usd(gas_today)}",
        f"累计 Gas:     {fmt_usd(gas_total)}",
        "",
        f"自动领取:     {'已开启' if auto_claim_on else '已关闭'}",
        f"通知渠道:     {'、'.join(channels) or '未配置'}",
    ]
    if failures:
        lines += ["", "近 24 小时失败的 job:"]
        for job, err in failures[:5]:
            lines.append(f"  · {job}: {err[:80]}")
    else:
        lines += ["", "近 24 小时没有失败的 job。"]

    if auto_claim_on:
        lines += ["", "⚠️ 自动领取处于开启状态，请确认白名单里的项目规则仍然允许。"]
    else:
        lines += ["", "本系统只做监测与提醒，不会自动发出任何链上交易。"]

    return Message(
        title=f"📊 每日汇总 — {day}",
        body="\n".join(lines),
        type="DAILY_SUMMARY",
    )


def delivery_test() -> Message:
    return Message(
        title="✅ 通知链路测试",
        body="\n".join([
            "这是一条来自 app.cli notify test 的测试消息。",
            "",
            "如果你收到了它，说明通知渠道配置正确。",
            "系统仍处于只监控状态，不会自动发出任何链上交易。",
        ]),
        type="TEST",
    )

