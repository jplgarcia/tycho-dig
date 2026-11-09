"""An outputs Merkle root computed here, independently of the guest and of the node.

The guest (libcmt) keeps its own outputs tree and writes the root into the TX buffer when it
accepts an input. Recomputing that root from the outputs the machine actually emitted tells a
machine that declared a wrong root (RT-1) apart from a node that computed it wrongly. This is
the cross-check Diego proposed: if our root agrees with the node, the machine is wrong; if it
agrees with the machine, the node is wrong.

The tree follows libcmt's merkle.c (machine-guest-tools v0.18.0) and the CLI's host-side check:
height 63 (ROLLUP_LOG2_MAX_OUTPUT_COUNT), leaf = keccak256(output), pristine leaf = 32 zero
bytes. It spans every accepted output since genesis, not only the current epoch. The keccak256
fallback is the standard-library one from the RT-1 evidence tool (outputs-root-crosscheck.py).
"""

from __future__ import annotations

import hashlib

HEIGHT = 63

# ---- keccak256 ----------------------------------------------------------------------------------
# OpenSSL >= 3.2 exposes KECCAK-256 (Ethereum padding) through hashlib; hashlib.sha3_256 is a
# different padding and must not be used. Without it, a pure-Python Keccak-f[1600] is used.

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1
_EMPTY = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _f(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256_py(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data) + b"\x01"
    while len(msg) % rate:
        msg += b"\x00"
    msg[-1] |= 0x80
    A = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        blk = msg[off : off + rate]
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(blk[8 * i : 8 * i + 8], "little")
        A = _f(A)
    return b"".join(A[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def _pick_keccak():
    try:
        if hashlib.new("KECCAK-256", b"").hexdigest() == _EMPTY:
            return lambda data: hashlib.new("KECCAK-256", data).digest()
    except ValueError:
        pass
    return keccak256_py


keccak256 = _pick_keccak()
assert keccak256(b"").hex() == _EMPTY and keccak256_py(b"").hex() == _EMPTY


# ---- the outputs tree -----------------------------------------------------------------------------


def _pristine() -> list[bytes]:
    out = [b"\x00" * 32]
    for _ in range(HEIGHT):
        out.append(keccak256(out[-1] * 2))
    return out


_PRISTINE = _pristine()


class OutputsTree:
    """Append-only frontier of the outputs tree; root() after any number of appends."""

    def __init__(self) -> None:
        self._state: list[bytes | None] = [None] * HEIGHT
        self.count = 0

    def append(self, output: bytes) -> None:
        node = keccak256(output)
        for level in range(HEIGHT):
            if self.count >> level & 1:
                node = keccak256(self._state[level] + node)
            else:
                self._state[level] = node
                break
        self.count += 1

    def root(self) -> bytes:
        node = _PRISTINE[0]
        for level in range(HEIGHT):
            if self.count >> level & 1:
                node = keccak256(self._state[level] + node)
            else:
                node = keccak256(node + _PRISTINE[level])
        return node


def outputs_root(outputs: list[bytes]) -> bytes:
    tree = OutputsTree()
    for output in outputs:
        tree.append(output)
    return tree.root()
