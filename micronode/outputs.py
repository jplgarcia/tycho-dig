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

    def to_json(self) -> dict:
        return {"count": self.count, "state": [h.hex() if h else None for h in self._state]}

    @classmethod
    def from_json(cls, data: dict) -> "OutputsTree":
        tree = cls()
        tree.count = data["count"]
        tree._state = [bytes.fromhex(h) if h else None for h in data["state"]]
        return tree

    @classmethod
    def from_frontier(cls, levels: list[bytes | None]) -> "OutputsTree":
        """The tree a frontier stored by the emulator's hash_tree.frontier_store describes.

        That frontier has HEIGHT + 1 levels; level L holds the root of a complete subtree of
        2^L leaves exactly when bit L of the leaf count is set, which is this class's state.
        The top level is set only in a full tree (2^63 outputs), which is not supported here.
        """
        if len(levels) != HEIGHT + 1:
            raise ValueError(f"outputs frontier has {len(levels)} levels, expected {HEIGHT + 1}")
        if levels[HEIGHT] is not None:
            raise ValueError("outputs frontier of a full tree")
        tree = cls()
        for level, node in enumerate(levels[:HEIGHT]):
            if node is not None:
                if len(node) != 32:
                    raise ValueError(f"outputs frontier level {level} is not a 32-byte hash")
                tree.count |= 1 << level
                tree._state[level] = node
        return tree

    def to_frontier(self) -> list[bytes | None]:
        return [self._state[level] if self.count >> level & 1 else None for level in range(HEIGHT)] + [None]

    def root(self) -> bytes:
        node = _PRISTINE[0]
        for level in range(HEIGHT):
            if self.count >> level & 1:
                node = keccak256(self._state[level] + node)
            else:
                node = keccak256(node + _PRISTINE[level])
        return node


def load_frontier(path) -> OutputsTree:
    """Read an outputs-frontier.json (a JSON array of base64 hashes or false, lowest level first)."""
    import base64
    import json
    from pathlib import Path

    levels = []
    for entry in json.loads(Path(path).read_text()):
        if entry is False or entry is None:
            levels.append(None)
        elif isinstance(entry, str) and entry.lower().startswith("0x"):
            levels.append(bytes.fromhex(entry[2:]))
        elif isinstance(entry, str):
            levels.append(base64.b64decode(entry, validate=True))
        else:
            raise ValueError(f"{path}: unexpected frontier entry {entry!r}")
    return OutputsTree.from_frontier(levels)


def store_frontier(tree: OutputsTree, path) -> None:
    """Write a tree's frontier the way hash_tree.frontier_store does (used to build test checkpoints)."""
    import base64
    import json
    from pathlib import Path

    levels = [base64.b64encode(h).decode() if h else False for h in tree.to_frontier()]
    Path(path).write_text(json.dumps(levels, indent=2) + "\n")


def outputs_root(outputs: list[bytes]) -> bytes:
    tree = OutputsTree()
    for output in outputs:
        tree.append(output)
    return tree.root()
