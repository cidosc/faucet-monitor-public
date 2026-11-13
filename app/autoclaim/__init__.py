"""自动领取（签名与广播）。

`adapters/` 刻意为空 —— 见该包的文档字符串。
"""

from app.autoclaim.adapters import (
    ADAPTERS,
    BuiltTransaction,
    ClaimAdapter,
    ClaimRequest,
    ClaimResult,
    adapter_is_registered,
    adapter_is_tested,
    get_adapter,
    registered_count,
)
from app.autoclaim.executor import ExecutionResult, execute_claim, idempotency_key
from app.autoclaim.keys import (
    SignerInfo,
    WalletKeyError,
    key_file_path,
    load_signing_key,
    signing_available,
)
from app.autoclaim.policy import GATE_NAMES, PolicyDecision, evaluate

__all__ = [
    "ADAPTERS",
    "BuiltTransaction",
    "ClaimAdapter",
    "ClaimRequest",
    "ClaimResult",
    "ExecutionResult",
    "GATE_NAMES",
    "PolicyDecision",
    "SignerInfo",
    "WalletKeyError",
    "adapter_is_registered",
    "adapter_is_tested",
    "evaluate",
    "execute_claim",
    "get_adapter",
    "idempotency_key",
    "key_file_path",
    "load_signing_key",
    "registered_count",
    "signing_available",
]

