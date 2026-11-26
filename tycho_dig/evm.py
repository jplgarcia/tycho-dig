"""ABI encoding and decoding of Cartesi Rollups inputs.

The InputBox stores every input as a call to
``EvmAdvance(uint256,address,address,uint256,uint256,uint256,uint256,bytes)``
(rollups-contracts ``common/Inputs.sol``). The machine reads exactly these
bytes, so a file holding them can be fed to ``cartesi-machine`` as is.
"""

from __future__ import annotations

from dataclasses import dataclass

# cast sig "EvmAdvance(uint256,address,address,uint256,uint256,uint256,uint256,bytes)"
EVM_ADVANCE_SELECTOR = bytes.fromhex("415bf363")
# cast sig-event "InputAdded(address,uint256,bytes)"
INPUT_ADDED_TOPIC = "0xc05d337121a6e8605c6ec0b72aa29c4210ffe6e5b9cefdd6a7058188a8f66f98"

WORD = 32


@dataclass
class EvmAdvance:
    chain_id: int
    app_contract: str
    msg_sender: str
    block_number: int
    block_timestamp: int
    prev_randao: int
    index: int
    payload: bytes


def _uint(value: int) -> bytes:
    if value < 0 or value >= 1 << 256:
        raise ValueError(f"uint256 out of range: {value}")
    return value.to_bytes(WORD, "big")


def _address(value: str) -> bytes:
    raw = bytes.fromhex(strip0x(value))
    if len(raw) != 20:
        raise ValueError(f"address must have 20 bytes: {value}")
    return raw.rjust(WORD, b"\0")


def strip0x(value: str) -> str:
    return value[2:] if value.lower().startswith("0x") else value


def encode_bytes(data: bytes) -> bytes:
    """Length word followed by the data padded to a whole number of words."""
    padding = (-len(data)) % WORD
    return _uint(len(data)) + data + b"\0" * padding


def encode_evm_advance(adv: EvmAdvance) -> bytes:
    head = b"".join(
        [
            _uint(adv.chain_id),
            _address(adv.app_contract),
            _address(adv.msg_sender),
            _uint(adv.block_number),
            _uint(adv.block_timestamp),
            _uint(adv.prev_randao),
            _uint(adv.index),
            _uint(8 * WORD),  # offset of the payload: right after the 8 head words
        ]
    )
    return EVM_ADVANCE_SELECTOR + head + encode_bytes(adv.payload)


def _word(data: bytes, i: int) -> int:
    return int.from_bytes(data[i * WORD : (i + 1) * WORD], "big")


def decode_evm_advance(raw: bytes) -> EvmAdvance:
    if raw[:4] != EVM_ADVANCE_SELECTOR:
        raise ValueError(f"not an EvmAdvance input (selector 0x{raw[:4].hex()})")
    body = raw[4:]
    offset = _word(body, 7)
    length = int.from_bytes(body[offset : offset + WORD], "big")
    payload = body[offset + WORD : offset + WORD + length]
    if len(payload) != length:
        raise ValueError("payload is truncated")
    return EvmAdvance(
        chain_id=_word(body, 0),
        app_contract="0x" + body[WORD + 12 : 2 * WORD].hex(),
        msg_sender="0x" + body[2 * WORD + 12 : 3 * WORD].hex(),
        block_number=_word(body, 3),
        block_timestamp=_word(body, 4),
        prev_randao=_word(body, 5),
        index=_word(body, 6),
        payload=payload,
    )


def decode_abi_bytes(data: bytes) -> bytes:
    """Decode the ``bytes`` field of a log's data (a single dynamic argument)."""
    offset = int.from_bytes(data[:WORD], "big")
    length = int.from_bytes(data[offset : offset + WORD], "big")
    out = data[offset + WORD : offset + WORD + length]
    if len(out) != length:
        raise ValueError("log data is truncated")
    return out
