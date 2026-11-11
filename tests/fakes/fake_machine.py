"""A stand-in `cartesi-machine` with state, for the watch tests (no emulator needed).

The input payload drives it (the bytes of input-N.bin are searched):
  "notices K"  accept and emit K outputs (each the input's bytes plus its number)
  "reject"     reject: the state reverts, no outputs reach the tree
  "badroot"    accept, but declare 0x5a..5a as the outputs root
  "exception"  raise an exception: the CLI stops with exit 1 and stores nothing

State hash model: H(template) = keccak("template"); accept: keccak(prev || input); reject: prev.
--load reads a stored machine (state.json) or, for the template, starts at H(template).
--store writes state.json with the hash and the outputs so far. Every call is logged to
FAKE_CALLS (one JSON line: begin, end, load) so tests can check what was resumed from.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from micronode.outputs import keccak256, outputs_root  # noqa: E402

TEMPLATE_HASH = "0x" + keccak256(b"template").hex()


def model(prev: str, data: bytes) -> tuple[str, str, list[bytes]]:
    """(outcome, hash after, outputs) of one input from state hash prev."""
    if b"exception" in data:
        return "tx-exception", "0x" + keccak256(bytes.fromhex(prev[2:]) + b"exc").hex(), []
    if b"reject" in data:
        return "rx-rejected", prev, []
    count = 1
    if b"notices " in data:
        count = int(data.split(b"notices ")[1].split()[0][:1])
    outs = [data + bytes([k]) for k in range(count)]
    return "rx-accepted", "0x" + keccak256(bytes.fromhex(prev[2:]) + data).hex(), outs


def main(args: list[str]) -> int:
    if "--help" in args:
        print("input_index_begin:<number>\ncheck_outputs_merkle_root:<boolean>\n  --remote-shutdown")
        return 0
    if "--version" in args:
        print("cartesi-machine 0.21.0")
        return 0
    opts = dict(a.split("=", 1) for a in args if a.startswith("--") and "=" in a)
    adv = dict(kv.split(":", 1) for kv in opts["--cmio-advance-state"].split(",") if ":" in kv)
    begin, end = int(adv["input_index_begin"]), int(adv["input_index_end"])
    load = Path(opts["--load"])
    with open(os.environ["FAKE_CALLS"], "a") as f:
        f.write(json.dumps({"begin": begin, "end": end, "load": str(load)}) + "\n")
    saved = load / "state.json"
    if saved.exists():
        st = json.loads(saved.read_text())
    else:
        st = {"hash": TEMPLATE_HASH, "outputs": []}
    log = sys.stderr
    print("Manual yield rx-accepted (1) (0x000020 data)", file=log)  # where the loaded machine sits
    out_index = 0  # without last_output_proof the CLI numbers this run's outputs from 0
    h, outputs = st["hash"], [bytes.fromhex(o) for o in st["outputs"]]
    for i in range(begin, end):
        data = Path(f"input-{i}.bin").read_bytes()
        print(f"Before input {i}\n0: {h}\n0: 0x{keccak256(bytes.fromhex(h[2:]) + b'loaded').hex()}", file=log)
        outcome, after, outs = model(h, data)
        print(f"Manual yield {outcome} (1) (0x000020 data)", file=log)
        if outcome == "tx-exception":
            print("lua5.4: input raised an exception", file=log)
            print("Shutdown JSONRPC remote cartesi machine at '127.0.0.1:1'", file=log)
            return 1
        if outcome == "rx-accepted":
            for o in outs:
                Path(f"out-{i}-{out_index}.bin").write_bytes(o)
                print(f"Storing out-{i}-{out_index}.bin", file=log)
                out_index += 1
            outputs += outs
            root = b"\x5a" * 32 if b"badroot" in data else outputs_root(outputs)
            Path(f"root-{i}.bin").write_bytes(root)
            print(f"Storing root-{i}.bin", file=log)
        h = after
    print(f"0: {h}", file=log)  # --final-hash
    if "--store" in opts:
        store = Path(opts["--store"])
        store.mkdir()
        (store / "config.json").write_text("{}")
        (store / "state.json").write_text(json.dumps({"hash": h, "outputs": [o.hex() for o in outputs]}))
    print("Shutdown JSONRPC remote cartesi machine at '127.0.0.1:1'", file=log)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
