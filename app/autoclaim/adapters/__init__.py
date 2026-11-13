"""领取适配器注册表。

**这个包在 v1 上空着，是刻意的安全设计。**

`app/autoclaim/policy.py` 的第 7 道门槛要求"存在已注册且测试过的适配器"。没有适配器时
`get_adapter()` 永远返回 None，第 7 道恒不通过，所以**任何自动链上动作都无法执行** ——
无论环境变量怎么设。

新增一个适配器是一个需要审查的动作。动手前先确认全部条件：

  1. 项目 Terms / Docs / FAQ 明确允许自动化领取。把原文字句记进
     `projects.automation_evidence`。
  2. 该水龙头是项目自己的官方端点（`faucets.is_official_source`）。
  3. 资源免费（`faucets.is_free_resource`）且不需要真实资金。
  4. 不涉及 CAPTCHA、Cloudflare 或其它人机验证。**适配器绝不允许尝试破解它们。**
  5. 领取是幂等的，且 cooldown 已知（`faucets.cooldown_seconds IS NOT NULL`）。

然后实现下面的 `ClaimAdapter`，注册进 `ADAPTERS`，并且只有在真的成功领取过一次之后
才把 `tested` 设为 True。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ClaimRequest:
    faucet_id: int
    faucet_url: str
    wallet_address: str
    chain_key: str
    idempotency_key: str


@dataclass
class BuiltTransaction:
    """一笔待签名的交易。适配器只负责**构造**，签名与广播由 executor 负责。"""

    ok: bool
    to_address: str | None = None
    data: str | None = None
    value: int = 0
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimResult:
    ok: bool
    tx_hash: str | None = None
    amount: str | None = None
    amount_unit: str | None = None
    next_available_at: str | None = None
    error: str | None = None
    evidence: dict[str, Any] | None = None


@runtime_checkable
class ClaimAdapter(Protocol):
    """单个水龙头的领取实现。

    实现必须幂等：同一个 `idempotency_key` 调用两次不能产生两次领取。
    """

    name: str
    tested: bool

    async def build_transaction(self, request: ClaimRequest, chain: Any) -> BuiltTransaction:
        """构造待签名交易。不应有任何副作用，也不应联网发交易。"""
        ...


# ── 注册表 ──
# 刻意为空。往这里加一条，就是打开了第 7 道门槛。
ADAPTERS: dict[str, ClaimAdapter] = {}


def get_adapter(faucet_url: str) -> ClaimAdapter | None:
    return ADAPTERS.get(faucet_url)


def adapter_is_registered(faucet_url: str) -> bool:
    return faucet_url in ADAPTERS


def adapter_is_tested(faucet_url: str) -> bool:
    a = ADAPTERS.get(faucet_url)
    return bool(a is not None and a.tested)


def registered_count() -> int:
    return len(ADAPTERS)

