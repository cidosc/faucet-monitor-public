"""ABI / keccak correctness.

Calldata is money, so these tests pin the implementation to values that are publicly
known and independently verifiable rather than to whatever this code happens to produce.
"""

from __future__ import annotations

import pytest

from app.claims.abi import (
    canonical_signature,
    decode_bool,
    decode_revert_reason,
    decode_string,
    decode_uint,
    encode_call,
    event_topic,
    looks_already_claimed,
    parse_signature,
    selector,
)


class TestKnownSelectors:
    """Selectors that appear in every ERC-20/721 contract and are widely documented."""

    @pytest.mark.parametrize(
        "signature,expected",
        [
            ("transfer(address,uint256)", "0xa9059cbb"),
            ("approve(address,uint256)", "0x095ea7b3"),
            ("balanceOf(address)", "0x70a08231"),
            ("totalSupply()", "0x18160ddd"),
            ("decimals()", "0x313ce567"),
            ("ownerOf(uint256)", "0x6352211e"),
            ("safeTransferFrom(address,address,uint256)", "0x42842e0e"),
        ],
    )
    def test_erc_standard_selectors(self, signature, expected):
        assert selector(signature) == expected

    def test_transfer_event_topic(self):
        # The canonical ERC-20 Transfer topic. If keccak is wrong this fails.
        assert event_topic("Transfer(address,address,uint256)") == (
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        )

    def test_approval_event_topic(self):
        assert event_topic("Approval(address,address,uint256)") == (
            "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
        )


class TestSignatureParsing:
    def test_parse(self):
        name, args = parse_signature("claim(address,uint256,bytes32[])")
        assert name == "claim"
        assert args == ["address", "uint256", "bytes32[]"]

    def test_parse_no_args(self):
        assert parse_signature("claim()") == ("claim", [])

    def test_alias_normalisation(self):
        # `uint` and `uint256` must produce the SAME selector, or we would call the
        # wrong function and still think we succeeded.
        assert canonical_signature("f(uint)") == "f(uint256)"
        assert selector("f(uint)") == selector("f(uint256)")

    @pytest.mark.parametrize("bad", ["", "noParens", "f(", "f(uint257)", "f(mapping)"])
    def test_malformed_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_signature(bad)


class TestEncoding:
    def test_selector_only_for_no_args(self):
        assert encode_call("claim()", []) == selector("claim()")

    def test_address_is_left_padded(self):
        data = encode_call("balanceOf(address)", ["0x000000000000000000000000000000000000dEaD"])
        assert data.startswith("0x70a08231")
        assert data.endswith("0" * 24 + "000000000000000000000000000000000000dead")

    def test_wrong_arg_count_rejected(self):
        with pytest.raises(ValueError):
            encode_call("claim(address,uint256)", ["0x" + "11" * 20])

    def test_bytes32_array_encoding(self):
        proof = ["0x" + "aa" * 32, "0x" + "bb" * 32]
        data = encode_call("claim(uint256,bytes32[])", [1000, proof])
        assert data.startswith(selector("claim(uint256,bytes32[])"))
        # 2-element dynamic array: length word present
        assert len(data) > 4 + 64 * 4
        assert "00" * 32 not in data[:10]

    def test_address_and_array_together(self):
        data = encode_call(
            "claim(address,uint256,bytes32[])",
            ["0x1111111111111111111111111111111111111111", 42, ["0x" + "cc" * 32]],
        )
        assert data.startswith(selector("claim(address,uint256,bytes32[])"))

    def test_bad_bytes32_length_rejected(self):
        with pytest.raises(ValueError):
            encode_call("claim(bytes32[])", [["0xdeadbeef"]])


class TestDecoding:
    def test_decode_uint(self):
        raw = "0x" + "0" * 63 + "5"
        assert decode_uint(raw) == 5

    def test_decode_uint_empty(self):
        assert decode_uint("0x") is None
        assert decode_uint(None) is None

    def test_decode_bool(self):
        assert decode_bool("0x" + "0" * 64) is False
        assert decode_bool("0x" + "0" * 63 + "1") is True
        assert decode_bool("0x") is None

    def test_decode_string(self):
        # abi-encoded "hello"
        raw = (
            "0x"
            + f"{32:064x}"
            + f"{5:064x}"
            + "68656c6c6f".ljust(64, "0")
        )
        assert decode_string(raw) == "hello"


class TestRevertReasons:
    def test_error_string_selector(self):
        # Error(string) with "Already claimed"
        msg = b"Already claimed"
        payload = (
            "0x08c379a0"
            + f"{32:064x}"
            + f"{len(msg):064x}"
            + msg.hex().ljust(64, "0")
        )
        assert decode_revert_reason(payload) == "Already claimed"

    def test_geth_style(self):
        assert decode_revert_reason("execution reverted: Not eligible") == "Not eligible"

    def test_panic(self):
        reason = decode_revert_reason("0x4e487b71" + f"{0x11:064x}")
        assert reason is not None and reason.startswith("panic")

    def test_none_input(self):
        assert decode_revert_reason(None) is None


class TestAlreadyClaimedHeuristic:
    @pytest.mark.parametrize(
        "reason",
        ["Already claimed", "already claimed!", "Duplicate claim", "Has claimed already"],
    )
    def test_positive(self, reason):
        assert looks_already_claimed(reason)

    @pytest.mark.parametrize("reason", ["Not eligible", "Invalid proof", None, ""])
    def test_negative(self, reason):
        assert not looks_already_claimed(reason)

