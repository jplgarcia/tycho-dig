"""Reading inputs from the InputBox and per-input hashes from a rollups-node."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from . import rpc
from .evm import INPUT_ADDED_TOPIC, decode_abi_bytes, strip0x


@dataclass
class ChainInput:
    index: int
    raw: bytes
    block_number: int
    transaction_hash: str


def _topic_address(address: str) -> str:
    return "0x" + strip0x(address).lower().rjust(64, "0")


def fetch_inputs(
    eth_rpc: str,
    inputbox: str,
    app: str,
    from_block: int,
    to_block: int | str = "finalized",
    chunk: int = 10_000,
) -> Iterator[ChainInput]:
    """Yield every InputAdded(app, ...) from the InputBox, in index order.

    Reads the chain directly, without going through any node, so a node that
    skipped, reordered or altered an input does not affect what we replay.
    ``to_block`` defaults to the finalized block: reading below finality would
    make the micronode itself raise false alarms on a reorg.
    """
    to_block = resolve_block(eth_rpc, to_block)
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        logs = rpc.call(
            eth_rpc,
            "eth_getLogs",
            [
                {
                    "address": inputbox,
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                    "topics": [INPUT_ADDED_TOPIC, _topic_address(app)],
                }
            ],
        )
        logs.sort(key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"], 16)))
        for log in logs:
            if log.get("removed"):
                continue
            yield ChainInput(
                index=int(log["topics"][2], 16),
                raw=decode_abi_bytes(bytes.fromhex(strip0x(log["data"]))),
                block_number=int(log["blockNumber"], 16),
                transaction_hash=log["transactionHash"],
            )
        start = end + 1


def resolve_block(eth_rpc: str, block: int | str) -> int:
    """A block number, or a tag (finalized, safe, latest) resolved to its number."""
    if isinstance(block, int):
        return block
    if block.isdigit() or block.startswith("0x"):
        return int(block, 0)
    head = rpc.call(eth_rpc, "eth_getBlockByNumber", [block, False])
    if head is None:
        raise RuntimeError(f"the RPC has no {block!r} block")
    return int(head["number"], 16)


def node_application(node_rpc: str, app: str) -> dict:
    """cartesi_getApplication: name/address, InputBox address and deployment block."""
    return rpc.call(node_rpc, "cartesi_getApplication", {"application": app})["data"]


def node_input_hashes(node_rpc: str, app: str, page: int = 500) -> dict[int, str | None]:
    """Per-input machine hash as the node recorded it (None while unprocessed).

    The node stores the machine root hash right after each advance
    (rollups-node pkg/machine Advance -> GetRootHash), which is what the
    machine CLI prints as the first hash "Before input N+1".
    """
    hashes: dict[int, str | None] = {}
    offset = 0
    while True:
        resp = rpc.call(
            node_rpc,
            "cartesi_listInputs",
            {"application": app, "limit": page, "offset": offset},
        )
        for item in resp["data"]:
            index = item["index"]
            index = int(index, 16) if isinstance(index, str) else int(index)
            mh = item.get("machine_hash")
            hashes[index] = mh.lower() if mh else None
        offset += len(resp["data"])
        if not resp["data"] or offset >= resp["pagination"]["total_count"]:
            return hashes
