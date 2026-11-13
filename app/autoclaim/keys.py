"""签名密钥加载。

安全要求（这个模块的存在意义就是把这几条固定下来）：
  * 私钥只从文件读，**绝不**从环境变量读 —— 环境变量会出现在 `docker inspect`、
    `/proc/<pid>/environ` 和崩溃报告里
  * 私钥**绝不**进入日志、异常消息、返回值或数据库
  * 私钥**绝不**被打印，即使出错
  * 地址不匹配时拒绝加载，而不是继续用

普通监控不需要调用本模块；执行器和显式 signer 自检命令可能加载密钥。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.logging_conf import get_logger

log = get_logger(__name__)

DEFAULT_KEY_FILE = "/run/secrets/wallet_key"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class WalletKeyError(RuntimeError):
    """密钥相关错误。异常消息里**永远不会**包含密钥内容。"""


@dataclass
class SignerInfo:
    """关于签名者的、可以安全展示的信息。不含密钥。"""

    address: str
    source: str
    key_fingerprint: str          # sha256 前 16 位，仅用于核对"是不是同一把钥匙"


def _fingerprint(raw: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def key_file_path() -> Path:
    return Path(os.environ.get("WALLET_KEY_FILE", DEFAULT_KEY_FILE))


def load_signing_key(*, expected_address: str | None = None) -> tuple[object, SignerInfo]:
    """加载并校验签名密钥。

    返回 `(LocalAccount, SignerInfo)`。LocalAccount 必须在调用方作用域内使用，
    不要存到全局或序列化。

    `expected_address` 不为空时会比对 —— 不匹配直接拒绝，避免用错钥匙签名。
    """
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover
        raise WalletKeyError(
            "eth-account 未安装 —— 自动领取不可用（这是刻意的：默认镜像不含签名能力）"
        ) from exc

    path = key_file_path()
    if not path.exists():
        raise WalletKeyError(
            f"密钥文件不存在: {path}。用 scripts/set-wallet-key.sh 安装，"
            "或设置 WALLET_KEY_FILE 指向正确位置。"
        )

    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WalletKeyError(
            f"无法读取密钥文件 {path}（{exc.strerror}）。"
            "常见原因：Docker Compose 的 secrets 是绑定挂载，会保留宿主权限，"
            "所以属主必须是容器用户的 uid（见 scripts/set-wallet-key.sh）。"
        ) from exc

    if raw.startswith("0x") or raw.startswith("0X"):
        raw = raw[2:]
    if not _HEX64.match(raw):
        # 只说格式，不透露内容长度之外的信息
        raise WalletKeyError(
            "密钥格式不正确：应为 64 位十六进制字符（可带 0x 前缀）"
        )

    try:
        account = Account.from_key("0x" + raw)
    except Exception as exc:  # noqa: BLE001
        raise WalletKeyError("密钥无法解析为有效的 secp256k1 私钥") from exc

    info = SignerInfo(
        address=account.address,
        source=str(path),
        key_fingerprint=_fingerprint(raw),
    )

    if expected_address and account.address.lower() != expected_address.lower():
        # 这里绝不能把两个地址之外的任何东西写进日志
        raise WalletKeyError(
            f"密钥对应的地址是 {account.address}，但期望的是 {expected_address}。"
            "拒绝加载 —— 用错钥匙签名会造成无法撤销的后果。"
        )

    log.info(
        "signing_key_loaded",
        extra={"address": info.address, "fingerprint": info.key_fingerprint,
               "source": info.source},
    )
    return account, info


def signing_available() -> bool:
    """不加载密钥，只判断"能不能加载"。用于状态展示和 gate 判定。"""
    path = key_file_path()
    if not path.exists():
        return False
    try:
        from eth_account import Account  # noqa: F401
    except ImportError:
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False

