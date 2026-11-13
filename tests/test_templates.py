"""通知模板测试。

重点两件事：
  1. 所有用户可见文案必须是中文（回归保护：这些曾经全是英文，散落在 5 个文件里）
  2. 诊断原因必须被翻译 —— 但代码里生成原因的字符串要保持英文，因为它们同时写进
     scan_logs 和 evidence，需要可 grep
"""

from __future__ import annotations

import re

import pytest

from app.notify import templates as T

CJK = re.compile(r"[\u4e00-\u9fff]")

# 允许出现的英文：字段标签里的技术名词、状态枚举、地址、URL。
# 这个白名单是刻意列出来的 —— 新增英文文案时会被迫在此处显式确认。
ALLOWED_LATIN = {
    "gas", "http", "rpc", "url", "eth_call", "usdt0", "erc20", "erc721", "erc1155",
    "recaptcha", "hcaptcha", "cloudflare", "turnstile", "geetest", "github", "discord",
    "google", "twitter", "robots.txt", "api", "to", "data", "main", "unknown",
    "available", "cooldown", "empty", "offline", "captcha", "manual_required",
    "window_not_open", "window_closed", "not_eligible", "unsimulated", "ready",
    "already_claimed", "feishu", "telegram", "testnet", "mainnet",
}


def _all_messages() -> list[tuple[str, T.Message]]:
    from types import SimpleNamespace

    fx = SimpleNamespace(
        requires_wallet_address=True, requires_login=False, login_provider=None,
        requires_captcha=False, captcha_type=None, cooldown_seconds=43200,
    )
    return [
        ("new_faucet", T.new_faucet(
            project="P", chain="Base", is_testnet=True, faucet_url="https://x.test/f",
            requirement=T.requirement_hint(fx), policy="UNKNOWN")),
        ("faucet_available", T.faucet_available(
            project="P", chain="Base", is_testnet=True, faucet_url="https://x.test/f",
            previous="COOLDOWN", confidence=0.7, requirement=T.requirement_hint(fx),
            policy="UNKNOWN")),
        ("faucet_failed", T.faucet_failed(
            project="P", faucet_url="https://x.test/f", status="OFFLINE",
            reason="server_error (HTTP 503)", http_status=503, consecutive=3)),
        ("claim_ready_testnet", T.claim_ready(
            project="P", target="T", chain="Base Sepolia", is_testnet=True,
            wallet_label="main", wallet_address="0xabc", gas_estimate=21000,
            gas_price_wei=10**9, estimated_usd=None, claim_url="https://c.test/",
            to_address="0xc1", calldata="0xdead", gas_acceptable=True,
            max_gwei=30.0, max_usd=5.0)),
        ("claim_ready_cheap", T.claim_ready(
            project="P", target="T", chain="Base", is_testnet=False,
            wallet_label="main", wallet_address="0xabc", gas_estimate=21000,
            gas_price_wei=10**9, estimated_usd="0.01", claim_url="https://c.test/",
            to_address="0xc1", calldata="0xdead", gas_acceptable=True,
            max_gwei=30.0, max_usd=5.0)),
        ("claim_ready_pricey", T.claim_ready(
            project="P", target="T", chain="Ethereum", is_testnet=False,
            wallet_label="main", wallet_address="0xabc", gas_estimate=21000,
            gas_price_wei=90 * 10**9, estimated_usd="12.5", claim_url="https://c.test/",
            to_address="0xc1", calldata="0xdead", gas_acceptable=False,
            max_gwei=30.0, max_usd=5.0)),
        ("claim_already", T.claim_already(
            project="P", target="T", wallet_label="main", evidence="Already claimed")),
        ("claim_not_available", T.claim_not_available(
            project="P", target="T", wallet_label="main",
            status="WINDOW_NOT_OPEN", reason="window has not started")),
        ("incoming_transfer", T.incoming_transfer(
            wallet_label="main", wallet_address="0xabc", chain="Plasma", is_testnet=False,
            asset="USDT0", amount="2.937962", contract="0xb8ce", from_address="0x51e6",
            tx_hash="0xabc", block_number=1, occurred_at=None, category="erc20")),
        ("daily_summary", T.daily_summary(
            day="2026-09-13", new_projects=1, faucet_status={"AVAILABLE": 1},
            pending_tasks=0, wallets=1, claims_ok=0, claims_total=0, gas_today=0,
            gas_total=0, auto_claim_on=False, channels=["feishu"], failures=[])),
        ("delivery_test", T.delivery_test()),
    ]


class TestEverythingIsChinese:
    @pytest.mark.parametrize("name", [n for n, _ in _all_messages()])
    def test_title_contains_chinese(self, name):
        msgs = dict(_all_messages())
        assert CJK.search(msgs[name].title), f"{name} 的标题没有中文"

    @pytest.mark.parametrize("name", [n for n, _ in _all_messages()])
    def test_body_is_mostly_chinese(self, name):
        msgs = dict(_all_messages())
        body = msgs[name].body
        assert CJK.search(body), f"{name} 的正文没有中文"
        # 中文字符应占非空白字符的相当比例，防止「标题中文、正文英文」这种半吊子
        non_space = [c for c in body if not c.isspace()]
        cjk_ratio = sum(1 for c in non_space if CJK.match(c)) / max(1, len(non_space))
        assert cjk_ratio > 0.25, f"{name} 正文中文比例过低（{cjk_ratio:.2f}）"


class TestReasonTranslation:
    @pytest.mark.parametrize(
        "raw,expect_fragment",
        [
            ("server_error (HTTP 503)", "服务器错误"),
            ("url_gone (HTTP 404)", "页面已不存在"),
            ("cloudflare_challenge_detected", "Cloudflare"),
            ("captcha_detected (recaptcha)", "验证码"),
            ("login_required (github)", "需要登录"),
            ("faucet_reports_empty", "已空"),
            ("cooldown_indicated, ~43200s", "冷却中"),
            ("wallet_address_form_present; no blocker detected", "钱包地址表单"),
            ("reachable_but_no_wallet_form_found", "未找到钱包地址表单"),
            ("maintenance_or_paused_notice", "维护"),
            ("window has not started", "窗口尚未开始"),
            ("window has ended", "窗口已结束"),
            ("Already claimed", "已领取"),
            ("human verification required (recaptcha)", "人机验证"),
            ("3 requirement(s) failed on base", "条件未满足"),
            ("rpc_not_configured (RPC_BASE)", "RPC 未配置"),
            ("access_control (HTTP 403: http_403 (nginx))", "访问被拒绝"),
            ("probe_exception: ValueError: boom", "探测异常"),
            ("downgraded_to_UNKNOWN (confidence 0.30 < 0.5)", "不做猜测"),
            ("no_response: ConnectTimeout: ", "无响应"),
        ],
    )
    def test_known_reasons_are_translated(self, raw, expect_fragment):
        out = T.reason_zh(raw)
        assert CJK.search(out), f"未翻译: {raw!r} -> {out!r}"
        assert expect_fragment in out, f"{raw!r} -> {out!r}（期望包含 {expect_fragment!r}）"

    def test_unknown_reason_passes_through_unchanged(self):
        """不认识的字符串必须原样返回，而不是被编造一个说法。"""
        weird = "some_brand_new_failure_mode_xyz"
        assert T.reason_zh(weird) == weird

    def test_none_and_empty(self):
        assert T.reason_zh(None) == "未知"
        assert T.reason_zh("") == "未知"

    def test_reasons_zh_joins_and_dedupes(self):
        out = T.reasons_zh(["server_error (HTTP 503)", "server_error (HTTP 503)",
                            "faucet_reports_empty"])
        assert out.count("；") == 1
        assert "服务器错误" in out and "已空" in out


class TestGasGuidance:
    def _kw(self, **over):
        base = dict(
            project="P", target="T", chain="Ethereum", is_testnet=False,
            wallet_label="w", wallet_address="0x1", gas_estimate=21000,
            claim_url=None, to_address=None, calldata=None,
            max_gwei=30.0, max_usd=5.0,
        )
        base.update(over)
        return base

    def test_cheap_gas_says_suitable(self):
        m = T.claim_ready(**self._kw(gas_price_wei=10**9, estimated_usd="0.01",
                                     gas_acceptable=True))
        assert "适合领取" in m.body

    def test_expensive_gas_advises_waiting(self):
        m = T.claim_ready(**self._kw(gas_price_wei=90 * 10**9, estimated_usd="12",
                                     gas_acceptable=False))
        assert "建议等降下来" in m.body
        assert "偏高" in m.title

    def test_testnet_has_no_real_cost(self):
        m = T.claim_ready(**self._kw(chain="Base Sepolia", is_testnet=True,
                                     gas_price_wei=10**9, estimated_usd=None,
                                     gas_acceptable=True))
        assert "无实际成本" in m.body

    def test_unknown_cost_admits_it(self):
        m = T.claim_ready(**self._kw(gas_price_wei=None, estimated_usd=None,
                                     gas_acceptable=None))
        assert "无法确定" in m.body

    def test_ready_always_states_it_is_not_an_action(self):
        for accept in (True, False):
            m = T.claim_ready(**self._kw(gas_price_wei=10**9, estimated_usd="0.01",
                                         gas_acceptable=accept))
            assert "不是操作" in m.body


class TestSafetyWording:
    def test_automation_hint_never_says_allowed_unless_verified(self):
        assert "已人工核实" in T.automation_hint("ALLOWED")
        assert "禁止" in T.automation_hint("FORBIDDEN")
        assert "未验证" in T.automation_hint("UNKNOWN")
        assert "未验证" in T.automation_hint(None)

    def test_incoming_transfer_warns_about_phishing(self):
        m = dict(_all_messages())["incoming_transfer"]
        assert "钓鱼" in m.body

    def test_daily_summary_states_no_chain_writes(self):
        m = dict(_all_messages())["daily_summary"]
        assert "不会自动发出任何链上交易" in m.body

