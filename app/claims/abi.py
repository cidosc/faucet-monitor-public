"""Minimal, dependency-backed ABI helpers.

Calldata is money. Hand-rolling ABI encoding is the standard way to lose it, so this
module delegates all encoding to `eth_abi` and all hashing to `eth_utils.keccak`
(which is Keccak-256, NOT hashlib's sha3_256 — they differ in padding).

Nothing here signs anything. This module only *builds* calldata for `eth_call`
simulation and for display to a human.
"""

from __future__ import annotations

import re
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from app.logging_conf import get_logger

log = get_logger(__name__)

_SIG_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$")

_SIMPLE_TYPES = {
    "address": "address",
    "bool": "bool",
    "bytes32": "bytes32",
    "bytes": "bytes",
    "string": "string",
    "uint256": "uint256",
    "uint": "uint256",
    "uint8": "uint8",
    "uint64": "uint64",
    "uint128": "uint128",
    "int256": "int256",
}


def parse_signature(signature: str) -> tuple[str, list[str]]:
    """``claim(address,uint256,bytes32[])`` -> ``('claim', ['address','uint256','bytes32[]'])``."""
    m = _SIG_RE.match(signature.strip())
    if not m:
        raise ValueError(f"malformed function signature: {signature!r}")
    name, raw_args = m.group(1), m.group(2).strip()
    args = [a.strip() for a in raw_args.split(",")] if raw_args else []
    for a in args:
        if a.endswith("[]"):
            base = a[:-2]
            if base not in _SIMPLE_TYPES:
                raise ValueError(f"unsupported array element type: {a!r}")
        elif a not in _SIMPLE_TYPES:
            raise ValueError(f"unsupported argument type: {a!r}")
    return name, args


def canonical_signature(signature: str) -> str:
    """Normalise aliases so the selector is always computed from canonical types."""
    name, args = parse_signature(signature)
    norm = []
    for a in args:
        if a.endswith("[]"):
            norm.append(_SIMPLE_TYPES[a[:-2]] + "[]")
        else:
            norm.append(_SIMPLE_TYPES[a])
    return f"{name}({','.join(norm)})"


def selector(signature: str) -> str:
    """4-byte function selector, e.g. ``0xa9059cbb`` for ``transfer(address,uint256)``."""
    return "0x" + keccak(text=canonical_signature(signature))[:4].hex()


def event_topic(signature: str) -> str:
    """32-byte event topic hash, e.g. for ``Transfer(address,address,uint256)``."""
    return "0x" + keccak(text=canonical_signature(signature)).hex()


def _coerce(arg_type: str, value: Any) -> Any:
    if arg_type == "address":
        return to_checksum_address(str(value))
    if arg_type.endswith("[]"):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"expected a list for {arg_type}, got {type(value).__name__}")
        return [_coerce(arg_type[:-2], v) for v in value]
    if arg_type == "bytes32":
        if isinstance(value, str):
            v = value[2:] if value.startswith("0x") else value
            if len(v) != 64:
                raise ValueError(f"bytes32 must be 32 bytes, got {len(v) // 2}")
            return bytes.fromhex(v)
        return bytes(value)
    if arg_type == "bytes":
        if isinstance(value, str) and value.startswith("0x"):
            return bytes.fromhex(value[2:])
        return bytes(value)
    if arg_type.startswith(("uint", "int")):
        return int(value)
    if arg_type == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
    return value


def encode_call(signature: str, args: list[Any]) -> str:
    """Build ``0x<selector><abi-encoded-args>`` for a function call."""
    name, arg_types = parse_signature(signature)
    if len(args) != len(arg_types):
        raise ValueError(
            f"{name} expects {len(arg_types)} argument(s) {arg_types}, got {len(args)}"
        )
    if not arg_types:
        return selector(signature)
    coerced = [_coerce(t, v) for t, v in zip(arg_types, args, strict=True)]
    return selector(signature) + abi_encode(arg_types, coerced).hex()


def decode_uint(return_data: str | None) -> int | None:
    """Decode a single uint256/uint8 return value. Returns None on empty data."""
    if not return_data or return_data in ("0x", ""):
        return None
    raw = return_data[2:] if return_data.startswith("0x") else return_data
    if len(raw) < 64:
        # Some contracts return a shorter encoding; left-pad it.
        raw = raw.rjust(64, "0")
    return int(raw[:64], 16)


def decode_bool(return_data: str | None) -> bool | None:
    v = decode_uint(return_data)
    return None if v is None else bool(v)


def decode_string(return_data: str | None) -> str | None:
    """Decode a solidity ``string`` return value (used for revert reasons)."""
    if not return_data or return_data in ("0x", ""):
        return None
    try:
        raw = bytes.fromhex(return_data[2:])
        if len(raw) < 64:
            return None
        offset = int.from_bytes(raw[:32], "big")
        length = int.from_bytes(raw[offset : offset + 32], "big")
        return raw[offset + 32 : offset + 32 + length].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def decode_revert_reason(error_payload: str | None) -> str | None:
    """Extract a human revert reason from an RPC error string.

    Handles the standard ``Error(string)`` selector (0x08c379a0) plus ``Panic(uint256)``
    (0x4e487b71), and falls back to surfacing the raw message.
    """
    if not error_payload:
        return None
    text = str(error_payload)

    m = re.search(r"0x08c379a0([0-9a-fA-F]+)", text)
    if m:
        try:
            raw = bytes.fromhex(m.group(1))
            offset = int.from_bytes(raw[:32], "big")
            length = int.from_bytes(raw[offset : offset + 32], "big")
            reason = raw[offset + 32 : offset + 32 + length].decode("utf-8", errors="replace")
            if reason:
                return reason
        except Exception:  # noqa: BLE001
            pass

    m = re.search(r"0x4e487b71([0-9a-fA-F]{64})", text)
    if m:
        return f"panic(0x{int(m.group(1), 16):x})"

    # geth-style: "execution reverted: Not eligible"
    m = re.search(r"execution reverted:?\s*(.*)", text)
    if m and m.group(1).strip():
        return m.group(1).strip()[:300]

    return text[:300] if text else None


def looks_already_claimed(reason: str | None) -> bool:
    """Best-effort classification of a revert reason. Used only to label a read-only
    observation — it never authorises an action."""
    if not reason:
        return False
    r = reason.lower()
    return any(
        k in r
        for k in ("already claimed", "alreadyclaimed", "claimed already", "has claimed",
                  "duplicate claim", "already redeemed")
    )

