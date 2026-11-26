"""Reading inputs from the InputBox and per-input hashes from a rollups-node."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from . import rpc
from .evm import INPUT_ADDED_TOPIC, decode_abi_bytes, strip0x
from .outputs import keccak256


@dataclass
class ChainInput:
    index: int
    raw: bytes
    block_number: int
    transaction_hash: str


def _topic_address(address: str) -> str:
    return "0x" + strip0x(address).lower().rjust(64, "0")


def get_logs(eth_rpc: str, address: str, topics: list, from_block: int, to_block: int, chunk: int = 10_000):
    """eth_getLogs over [from_block, to_block] in chunks, in chain order, without removed logs."""
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        logs = rpc.call(
            eth_rpc,
            "eth_getLogs",
            [{"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": topics}],
        )
        logs.sort(key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"], 16)))
        yield from (log for log in logs if not log.get("removed"))
        start = end + 1


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
    for log in get_logs(eth_rpc, inputbox, [INPUT_ADDED_TOPIC, _topic_address(app)], from_block, to_block, chunk):
        yield ChainInput(
            index=int(log["topics"][2], 16),
            raw=decode_abi_bytes(bytes.fromhex(strip0x(log["data"]))),
            block_number=int(log["blockNumber"], 16),
            transaction_hash=log["transactionHash"],
        )


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


# cast sig "getTemplateHash()" (rollups-contracts IApplication)
GET_TEMPLATE_HASH = "0x61b12c66"


def selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode())[:4].hex()


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


def call_word(eth_rpc: str, to: str, signature: str, block: str = "latest") -> bytes:
    """The first 32-byte word an argument-less view function returns."""
    out = bytes.fromhex(strip0x(rpc.call(eth_rpc, "eth_call", [{"to": to, "data": selector(signature)}, block])))
    if len(out) < 32:
        raise RuntimeError(f"{signature} on {to} returned {len(out)} bytes")
    return out[:32]


def call_address(eth_rpc: str, to: str, signature: str) -> str:
    return "0x" + call_word(eth_rpc, to, signature)[12:].hex()


# Dave (cartesi-rollups-prt v3.0.0-alpha.8) DaveConsensus
EPOCH_SEALED_TOPIC = event_topic("EpochSealed(uint256,uint256,uint256,bytes32,bytes32,address)")
EPOCH_STAGED_TOPIC = event_topic("EpochStaged(uint256,bytes32,bytes32)")


@dataclass
class EpochSealed:
    """An epoch's input range and the state it starts from (the previous epoch's settled end).

    epoch 0 is sealed at deployment, from the template, with outputs_merkle_root zero.
    """

    epoch: int
    input_lower: int
    input_upper: int  # exclusive
    initial_machine_state_hash: str
    outputs_merkle_root: str  # the output history the epoch starts from
    tournament: str
    block_number: int


@dataclass
class EpochStaged:
    """The tournament result staged for an epoch: the state and outputs root after its last input."""

    epoch: int
    post_epoch_machine_state_hash: str
    post_epoch_outputs_merkle_root: str
    block_number: int


def _words(log: dict) -> list[bytes]:
    data = bytes.fromhex(strip0x(log["data"]))
    return [data[i : i + 32] for i in range(0, len(data), 32)]


def epoch_events(eth_rpc: str, consensus: str, epochs: list[int] | None, from_block: int, to_block: int):
    """EpochSealed and EpochStaged of the given epochs (None: all): ({epoch: EpochSealed}, {epoch: EpochStaged})."""
    sealed, staged = {}, {}
    topics = [[EPOCH_SEALED_TOPIC, EPOCH_STAGED_TOPIC]]
    if epochs is not None:
        topics.append(["0x" + n.to_bytes(32, "big").hex() for n in epochs])
    for log in get_logs(eth_rpc, consensus, topics, from_block, to_block):
        epoch, w, block = int(log["topics"][1], 16), _words(log), int(log["blockNumber"], 16)
        if log["topics"][0] == EPOCH_SEALED_TOPIC:
            if epoch in sealed:
                raise RuntimeError(f"epoch {epoch} sealed twice (blocks {sealed[epoch].block_number}, {block})")
            sealed[epoch] = EpochSealed(
                epoch, int.from_bytes(w[0], "big"), int.from_bytes(w[1], "big"),
                "0x" + w[2].hex(), "0x" + w[3].hex(), "0x" + w[4][12:].hex(), block,
            )
        else:
            staged[epoch] = EpochStaged(epoch, "0x" + w[0].hex(), "0x" + w[1].hex(), block)
    return sealed, staged


def onchain_template_hash(eth_rpc: str, app: str) -> str:
    """The template hash the application contract was deployed with."""
    out = rpc.call(eth_rpc, "eth_call", [{"to": app, "data": GET_TEMPLATE_HASH}, "latest"])
    return "0x" + strip0x(out).lower()[-64:]


def node_application(node_rpc: str, app: str) -> dict:
    """cartesi_getApplication: name/address, InputBox address and deployment block."""
    return rpc.call(node_rpc, "cartesi_getApplication", {"application": app})["data"]


def node_epoch(node_rpc: str, app: str, index: int) -> dict:
    """cartesi_getEpoch: inputs [input_index_lower_bound, input_index_upper_bound), status, roots."""
    return rpc.call(node_rpc, "cartesi_getEpoch", {"application": app, "epoch_index": hex(index)})["data"]


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
