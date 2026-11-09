"""micronode: replay Cartesi Rollups inputs on a bare machine and compare hashes.

    micronode craft   build an input by hand (to simulate a situation)
    micronode fetch   read the inputs of an application from the InputBox
    micronode run     replay input files from the template, print per-input hashes
    micronode compare compare a run with the machine hashes a rollups-node recorded
    micronode check   fetch + run + compare in one go

Exit codes of compare/check: 0 all well, 2 divergence, 3 the node stopped making
progress (app status not OK, disabled, or a finalized input left unprocessed),
4 setup mismatch: the micronode's template state hash is not the application's
(another emulator version or template), so nothing was compared.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import chain, machine
from .evm import EvmAdvance, decode_evm_advance, encode_evm_advance, strip0x

RUN_FILE = "run.json"
FETCH_FILE = "inputs.json"


def _int(text: str) -> int:
    return int(text, 0)


def _input_indices(workdir: Path) -> list[int]:
    out = []
    for path in workdir.glob("input-*.bin"):
        stem = path.stem.removeprefix("input-")
        if stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def _contiguous_end(indices: list[int]) -> int:
    """First missing index starting from 0 (the machine needs every input in order)."""
    present = set(indices)
    n = 0
    while n in present:
        n += 1
    return n


# --- craft ---------------------------------------------------------------


def cmd_craft(args) -> int:
    workdir = Path(args.dir)
    workdir.mkdir(parents=True, exist_ok=True)
    index = args.index if args.index is not None else _contiguous_end(_input_indices(workdir))
    if args.payload_text is not None:
        payload = args.payload_text.encode()
    elif args.payload_file is not None:
        payload = Path(args.payload_file).read_bytes()
    else:
        payload = bytes.fromhex(strip0x(args.payload or ""))
    adv = EvmAdvance(
        chain_id=args.chain_id,
        app_contract=args.app,
        msg_sender=args.sender,
        block_number=args.block_number,
        block_timestamp=args.block_timestamp,
        prev_randao=args.prev_randao,
        index=index,
        payload=payload,
    )
    path = workdir / f"input-{index}.bin"
    path.write_bytes(encode_evm_advance(adv))
    print(f"wrote {path} (index {index}, payload {len(payload)} bytes)")
    return 0


# --- fetch ---------------------------------------------------------------


def _resolve_app(args) -> tuple[str, str, int]:
    """Return (application address, InputBox address, first block)."""
    app, inputbox, from_block = args.app, args.inputbox, args.from_block
    if args.node and (inputbox is None or from_block is None or not app.startswith("0x")):
        info = chain.node_application(args.node, app)
        app = info["iapplication_address"]
        inputbox = inputbox or info["iinputbox_address"]
        if from_block is None:
            block = info.get("iinputbox_block", 0)
            from_block = int(block, 16) if isinstance(block, str) else int(block)
    if inputbox is None:
        sys.exit("--inputbox is required without --node")
    return app, inputbox, from_block or 0


def do_fetch(args, workdir: Path) -> int:
    app, inputbox, from_block = _resolve_app(args)
    to_block = chain.resolve_block(args.rpc, args.to_block)
    workdir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for item in chain.fetch_inputs(args.rpc, inputbox, app, from_block, to_block):
        (workdir / f"input-{item.index}.bin").write_bytes(item.raw)
        manifest.append(
            {"index": item.index, "block_number": item.block_number, "transaction_hash": item.transaction_hash}
        )
    (workdir / FETCH_FILE).write_text(
        json.dumps({"app": app, "inputbox": inputbox, "to_block": to_block, "inputs": manifest}, indent=2)
    )
    print(f"fetched {len(manifest)} inputs of {app} from InputBox {inputbox} up to block {to_block} ({args.to_block})")
    return len(manifest)


def cmd_fetch(args) -> int:
    do_fetch(args, Path(args.dir))
    return 0


# --- run -----------------------------------------------------------------


def do_run(args, workdir: Path) -> machine.RunResult:
    indices = _input_indices(workdir)
    end = args.end if args.end is not None else _contiguous_end(indices)
    if end == 0:
        sys.exit(f"no input-0.bin in {workdir}")
    if args.end is None and len(indices) > end:
        print(f"warning: input-{end}.bin is missing; replaying 0..{end - 1} only", file=sys.stderr)
    result = machine.run(Path(args.template), workdir, 0, end, image=args.image, native=args.native)
    (workdir / RUN_FILE).write_text(
        json.dumps(
            {
                "template": str(args.template),
                "emulator_version": result.emulator_version,
                "initial_hash": result.initial_hash,
                "final_hash": result.final_hash,
                "inputs": [asdict(i) for i in result.inputs],
            },
            indent=2,
        )
    )
    (workdir / "run.log").write_text(result.log)
    return result


def _print_run(result: machine.RunResult) -> None:
    print(f"emulator {result.emulator_version or '?'}, template state hash {result.initial_hash or '?'}")
    print(f"{'input':>6}  {'outcome':<12} {'machine hash after input':<66}  outputs root")
    for item in result.inputs:
        print(
            f"{item.index:>6}  {item.outcome or '?':<12} {item.hash_after or '?':<66}  "
            f"{item.outputs_merkle_root or '-'}"
        )


def cmd_run(args) -> int:
    result = do_run(args, Path(args.dir))
    _print_run(result)
    return 0


# --- compare -------------------------------------------------------------


DEFAULT_MAX_LAG_BLOCKS = 25


def status_alarms(node_rpc: str, app: str) -> list[str]:
    """The node stopped working on the app: a status other than OK, or disabled.

    FAILED is recoverable but needs an operator; the others are terminal.
    """
    info = chain.node_application(node_rpc, app)
    alarms = []
    status = info.get("status")
    if status != "OK":
        reason = (info.get("reason") or "").replace("\n", " | ")
        alarms.append(f"app status is {status}: {reason[:300]}")
    if not info.get("enabled", True):
        alarms.append("app is disabled in the node")
    return alarms


def lag_alarms(workdir: Path, theirs: dict, max_lag_blocks: int) -> list[str]:
    """Finalized inputs the node has not processed after max_lag_blocks blocks."""
    manifest_path = workdir / FETCH_FILE
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text())
    to_block = manifest.get("to_block")
    if to_block is None:
        return []
    late = [
        (item["index"], to_block - item["block_number"])
        for item in manifest["inputs"]
        if theirs.get(item["index"]) is None and to_block - item["block_number"] > max_lag_blocks
    ]
    if not late:
        return []
    index, lag = late[0]
    return [
        f"node has not processed {len(late)} finalized input(s); "
        f"input {index} has been final for {lag} blocks (limit {max_lag_blocks})"
    ]


SETUP_MISMATCH = 4


def template_guard(initial_hash: str | None, expected: str | None, where: str, emulator: str | None) -> bool:
    """True when the micronode ran the same machine the application was deployed with.

    A different emulator version (0.23 changes the default MIMPID, so every state
    hash changes) or a different template gives a different initial hash. Then
    every per-input hash differs with no bug anywhere, so we refuse to compare.
    """
    if not initial_hash or not expected:
        print(f"warning: cannot verify the template ({where}: {expected}, micronode: {initial_hash})")
        return True
    if initial_hash.lower() == expected.lower():
        print(f"template matches {where} ({expected})")
        return True
    print(
        f"SETUP MISMATCH: the micronode's template state hash {initial_hash} is not {where} {expected}.\n"
        f"  The micronode (emulator {emulator or '?'}) is not running the application's machine: a different\n"
        f"  emulator version (state hashes change across versions) or a different template. Not comparing;\n"
        f"  use the emulator version and template the application was deployed with."
    )
    return False


def do_compare(
    node_rpc: str,
    app: str,
    workdir: Path,
    max_lag_blocks: int | None = DEFAULT_MAX_LAG_BLOCKS,
    expected_template: str | None = None,
) -> int:
    """Exit 0 all well, 2 divergence, 3 no progress, 4 setup mismatch (nothing compared)."""
    run = json.loads((workdir / RUN_FILE).read_text())
    if expected_template is None:
        expected_template, where = chain.node_application(node_rpc, app).get("template_hash"), "the node's template hash"
    else:
        where = "the on-chain template hash"
    if not template_guard(run.get("initial_hash"), expected_template, where, run.get("emulator_version")):
        return SETUP_MISMATCH
    alarms = status_alarms(node_rpc, app)
    ours = {item["index"]: item["hash_after"] for item in run["inputs"]}
    theirs = chain.node_input_hashes(node_rpc, app)
    if max_lag_blocks is not None:
        alarms += lag_alarms(workdir, theirs, max_lag_blocks)
    first_bad = None
    pending = 0
    for index in sorted(ours):
        node_hash = theirs.get(index)
        mine = (ours[index] or "").lower()
        if node_hash is None:
            status = "node has not processed it" if index in theirs else "node does not know it"
            pending += 1
        elif node_hash == mine:
            status = "ok"
        else:
            status = "DIVERGED"
            if first_bad is None:
                first_bad = index
        if status != "ok" or index == first_bad:
            print(f"input {index}: {status}\n  micronode {mine}\n  node      {node_hash}")
    extra = sorted(set(theirs) - set(ours))
    if extra:
        print(f"node has {len(extra)} inputs the micronode did not replay (from {extra[0]})")
    for alarm in alarms:
        print(f"PROGRESS ALARM: {alarm}")
    if first_bad is not None:
        print(f"DIVERGENCE: first at input {first_bad}")
        return 2
    print(f"agree on {len(ours) - pending} inputs ({pending} not yet processed by the node)")
    return 3 if alarms else 0


def _max_lag(args) -> int | None:
    return None if args.max_lag_blocks < 0 else args.max_lag_blocks


def cmd_compare(args) -> int:
    return do_compare(args.node, args.app, Path(args.dir), _max_lag(args))


def cmd_check(args) -> int:
    workdir = Path(args.dir)
    if do_fetch(args, workdir) == 0:
        # nothing final yet is a normal state for a watcher; the node's status still matters
        print("no finalized inputs yet; nothing to compare")
        alarms = status_alarms(args.node, args.app)
        for alarm in alarms:
            print(f"PROGRESS ALARM: {alarm}")
        return 3 if alarms else 0
    result = do_run(args, workdir)
    _print_run(result)
    app_address = json.loads((workdir / FETCH_FILE).read_text())["app"]
    expected = chain.onchain_template_hash(args.rpc, app_address)
    return do_compare(args.node, args.app, workdir, _max_lag(args), expected_template=expected)


# --- show ----------------------------------------------------------------


def cmd_show(args) -> int:
    adv = decode_evm_advance(Path(args.file).read_bytes())
    d = asdict(adv)
    payload = d.pop("payload")
    for key, value in d.items():
        print(f"{key:>16}: {value}")
    print(f"{'payload':>16}: 0x{payload.hex()}")
    try:
        print(f"{'payload (text)':>16}: {payload.decode()}")
    except UnicodeDecodeError:
        pass
    return 0


# --- argument parsing ----------------------------------------------------


def _add_machine(p) -> None:
    p.add_argument("--template", required=True, help="machine snapshot directory of the application")
    p.add_argument("--image", default=machine.DEFAULT_IMAGE, help="emulator Docker image (default %(default)s)")
    p.add_argument("--native", action="store_true", help="use cartesi-machine from PATH instead of Docker")
    p.add_argument("--end", type=int, help="replay inputs 0..END-1 (default: every contiguous input file)")


def _add_fetch(p) -> None:
    p.add_argument("--rpc", required=True, help="Ethereum JSON-RPC URL (ideally not the node's provider)")
    p.add_argument("--app", required=True, help="application address (or name, with --node)")
    p.add_argument("--inputbox", help="InputBox address (taken from --node when omitted)")
    p.add_argument("--from-block", type=_int, help="first block to scan (default: the app's InputBox block)")
    p.add_argument(
        "--to-block",
        default="finalized",
        help="last block to scan: a number or finalized/safe/latest (default: %(default)s)",
    )


def _add_lag(p) -> None:
    p.add_argument(
        "--max-lag-blocks",
        type=int,
        default=DEFAULT_MAX_LAG_BLOCKS,
        help="alarm when a finalized input is still unprocessed this many blocks later "
        "(default %(default)s; negative disables)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="micronode", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("craft", help="write an EvmAdvance input file by hand")
    p.add_argument("--dir", required=True)
    p.add_argument("--index", type=int, help="input index (default: next free in --dir)")
    p.add_argument("--chain-id", type=_int, default=31337)
    p.add_argument("--app", default="0x" + "00" * 20)
    p.add_argument("--sender", default="0x" + "00" * 20)
    p.add_argument("--block-number", type=_int, default=0)
    p.add_argument("--block-timestamp", type=_int, default=0)
    p.add_argument("--prev-randao", type=_int, default=0)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--payload", help="payload as hex")
    g.add_argument("--payload-text", help="payload as UTF-8 text")
    g.add_argument("--payload-file", help="payload read from a file")
    p.set_defaults(func=cmd_craft)

    p = sub.add_parser("show", help="decode an input file")
    p.add_argument("file")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("fetch", help="read the app's inputs from the InputBox")
    p.add_argument("--dir", required=True)
    _add_fetch(p)
    p.add_argument("--node", help="rollups-node JSON-RPC, to look up the app's InputBox and block")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("run", help="replay input files and print per-input hashes")
    p.add_argument("--dir", required=True)
    _add_machine(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="compare the last run with the node's per-input machine hashes")
    p.add_argument("--dir", required=True)
    p.add_argument("--node", required=True, help="rollups-node JSON-RPC URL")
    p.add_argument("--app", required=True, help="application name or address as the node knows it")
    _add_lag(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("check", help="fetch + run + compare (exit 2 divergence, 3 no progress)")
    p.add_argument("--dir", required=True)
    _add_fetch(p)
    _add_machine(p)
    p.add_argument("--node", required=True, help="rollups-node JSON-RPC URL")
    _add_lag(p)
    p.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
