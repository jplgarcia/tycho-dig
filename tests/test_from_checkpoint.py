"""from-checkpoint: replay up to input K from a crisium checkpoint (fake 0.23 CLI, fake chain and node)."""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from tycho_dig import chain, cli
from tycho_dig.evm import INPUT_ADDED_TOPIC, EvmAdvance, encode_bytes, encode_evm_advance
from tycho_dig.outputs import OutputsTree, keccak256, load_frontier, store_frontier

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import fake_machine  # noqa: E402

FAKES = Path(__file__).parent / "fakes"
APP = "0x" + "ab" * 20
INPUTBOX = "0x" + "cd" * 20
CONSENSUS = "0x" + "ef" * 20
ZERO = "0x" + "00" * 32
PRISTINE = "0x" + OutputsTree().root().hex()


def word(value) -> bytes:
    if isinstance(value, int):
        return value.to_bytes(32, "big")
    return bytes.fromhex(value[2:]).rjust(32, b"\0")


def root_of(outputs) -> str:
    tree = OutputsTree()
    for o in outputs:
        tree.append(o)
    return "0x" + tree.root().hex()


class Chain:
    """Epochs 0-3 sealed (0 empty at deployment), 0-2 staged; epoch 3 in dispute; epoch 4 open.

    inputs 0-1 epoch 1, 2-6 epoch 2, 7-8 epoch 3, 9-10 epoch 4 (open).
    """

    EPOCHS = {
        1: (3, [b"notices 1 a", b"notices 2 b"]),
        2: (11, [b"notices 2 c", b"reject d", b"notices 1 e", b"notices 3 f", b"notices 1 g"]),
        3: (22, [b"notices 1 h", b"notices 2 i"]),
        4: (33, [b"notices 1 j", b"notices 1 k"]),
    }
    SEAL_BLOCKS = {0: 2, 1: 10, 2: 20, 3: 31}

    def __init__(self, epoch2=None):
        epochs = dict(self.EPOCHS)
        if epoch2 is not None:
            epochs[2] = (11, epoch2)
        self.inputs, bounds = [], {0: (0, 0)}  # (block, raw); epoch -> [lower, upper)
        for e, (block, payloads) in sorted(epochs.items()):
            lower = len(self.inputs)
            for k, payload in enumerate(payloads):
                self.add(block + k, payload)
            bounds[e] = (lower, len(self.inputs))
        self.bounds = bounds
        self.states = self.replay()  # state after each input: (hash, outputs since genesis)
        self.finalized = 40
        self.sealed = {}  # epoch: (block, lower, upper, initial hash, outputs root)
        for e, block in self.SEAL_BLOCKS.items():
            h, outs = self.start(e)
            self.sealed[e] = (block, *bounds[e], h, ZERO if e == 0 else root_of(outs))
        self.staged = {}  # epoch: (block, state, outputs root)
        for e in (0, 1, 2):
            h, outs = self.start(e + 1)
            self.staged[e] = (self.SEAL_BLOCKS[e + 1] - 1, h, root_of(outs))
        self.node_hashes = {i: h for i, (h, _) in enumerate(self.states)}

    def start(self, epoch):
        lower = self.bounds[epoch][0]
        return (fake_machine.TEMPLATE_HASH, []) if lower == 0 else self.states[lower - 1]

    def add(self, block, payload):
        adv = EvmAdvance(31337, APP, "0x" + "11" * 20, block, 1700000000 + block, 0, len(self.inputs), payload)
        self.inputs.append((block, encode_evm_advance(adv)))

    def replay(self):
        h, outs, states = fake_machine.TEMPLATE_HASH, [], []
        for _, raw in self.inputs:
            outcome, h2, new = fake_machine.model(h, raw)
            if outcome == "rx-accepted":
                outs = outs + new
            h = h2
            states.append((h, outs))
        return states

    def checkpoint(self, directory: Path, epoch: int):
        """What crisium stores in checkpoints/epoch-N (the fake machine reads state.json)."""
        directory.mkdir(parents=True)
        h, outs = self.start(epoch)
        (directory / "config.json").write_text("{}")
        (directory / "state.json").write_text(json.dumps({"hash": h, "outputs": [o.hex() for o in outs]}))
        tree = OutputsTree()
        for o in outs:
            tree.append(o)
        store_frontier(tree, directory / "outputs-frontier.json")
        previous = self.bounds.get(epoch - 1, (0, 0))
        (directory / "epoch.json").write_text(json.dumps({
            "version": 2, "initial_state_hash": "0x" + "77" * 32, "final_state_hash": h,
            "outputs_merkle_root": "0x" + tree.root().hex(), "output_count": tree.count,
            "input_count": previous[1] - previous[0], "log2_mcycles_per_period": 10}, indent=2))

    def logs(self, f):
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        if f["address"] == INPUTBOX:
            assert f["topics"][0] == INPUT_ADDED_TOPIC
            return [
                {"blockNumber": hex(b), "logIndex": "0x0", "transactionHash": "0x" + f"{i:064x}",
                 "topics": [INPUT_ADDED_TOPIC, "0x" + APP[2:].rjust(64, "0"), hex(i)],
                 "data": "0x" + ((32).to_bytes(32, "big") + encode_bytes(raw)).hex()}
                for i, (b, raw) in enumerate(self.inputs) if lo <= b <= hi
            ]
        assert f["address"] == CONSENSUS
        kinds = f["topics"][0]
        wanted = {int(t, 16) for t in f["topics"][1]} if len(f["topics"]) > 1 else None
        out = []
        for epoch, (b, lower, upper, h, root) in self.sealed.items():
            if chain.EPOCH_SEALED_TOPIC in kinds and (wanted is None or epoch in wanted) and lo <= b <= hi:
                data = word(lower) + word(upper) + word(h) + word(root) + word("0x" + "99" * 20)
                out.append({"blockNumber": hex(b), "logIndex": "0x0", "data": "0x" + data.hex(),
                            "topics": [chain.EPOCH_SEALED_TOPIC, "0x" + word(epoch).hex()]})
        for epoch, (b, h, root) in self.staged.items():
            if chain.EPOCH_STAGED_TOPIC in kinds and (wanted is None or epoch in wanted) and lo <= b <= hi:
                out.append({"blockNumber": hex(b), "logIndex": "0x1", "data": "0x" + (word(h) + word(root)).hex(),
                            "topics": [chain.EPOCH_STAGED_TOPIC, "0x" + word(epoch).hex()]})
        return out

    def handle(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.finalized)}
        if method == "eth_call":
            call = params[0]
            answers = {
                (APP, chain.selector("getOutputsMerkleRootValidator()")): word(CONSENSUS),
                (CONSENSUS, chain.selector("getInputBox()")): word(INPUTBOX),
                (CONSENSUS, chain.selector("getDeploymentBlockNumber()")): word(1),
            }
            return "0x" + answers[(call["to"], call["data"])].hex()
        if method == "eth_getLogs":
            return self.logs(params[0])
        if method == "cartesi_listInputs":
            items = [{"index": hex(i), "machine_hash": self.node_hashes.get(i)} for i in range(len(self.inputs))]
            return {"data": items[params["offset"]:], "pagination": {"total_count": len(items)}}
        raise AssertionError(method)


class FrontierTest(unittest.TestCase):
    def test_roundtrip_any_count(self):
        for count in (0, 1, 2, 3, 5, 8, 13):
            tree = OutputsTree()
            for k in range(count):
                tree.append(bytes([k]) * 7)
            with tempfile.TemporaryDirectory() as tmp:
                store_frontier(tree, Path(tmp) / "f.json")
                levels = json.loads((Path(tmp) / "f.json").read_text())
                self.assertEqual(len(levels), 64)
                loaded = load_frontier(Path(tmp) / "f.json")
            self.assertEqual((loaded.count, loaded.root()), (tree.count, tree.root()))
            loaded.append(b"next")
            tree.append(b"next")
            self.assertEqual(loaded.root(), tree.root())

    def test_event_topics(self):
        self.assertEqual(chain.EPOCH_SEALED_TOPIC, "0x" + keccak256(
            b"EpochSealed(uint256,uint256,uint256,bytes32,bytes32,address)").hex())
        self.assertEqual(chain.selector("getTemplateHash()"), chain.GET_TEMPLATE_HASH)


def set_read_only(directory: Path, read_only: bool = True) -> None:
    for p in [*directory.rglob("*"), directory]:
        mode = p.stat().st_mode
        if read_only:
            p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        else:
            p.chmod(mode | stat.S_IWUSR)


class FromCheckpointTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.crisium = self.tmp / "crisium"
        self.crisium.mkdir()
        self.addCleanup(set_read_only, self.crisium, False)  # so the temporary directory can go
        (self.tmp / "bin").mkdir()
        exe = self.tmp / "bin" / "cartesi-machine"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "fake_machine.py"}" "$@"\n')
        exe.chmod(0o755)
        self.calls = self.tmp / "calls.jsonl"
        env = mock.patch.dict(os.environ, {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                                           "FAKE_CALLS": str(self.calls), "FAKE_EMULATOR": "0.23"})
        env.start()
        self.addCleanup(env.stop)
        self.use(Chain())

    def use(self, world):
        self.world = world

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                   "result": world.handle(req["method"], req["params"])}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def crisium_stores(self, *epochs, tamper=None):
        """crisium's checkpoints directory, read-only for tycho-dig, as a :ro mount makes it."""
        set_read_only(self.crisium, False)
        for e in epochs:
            if not (self.crisium / f"epoch-{e}").exists():
                self.world.checkpoint(self.crisium / f"epoch-{e}", e)
                if tamper:
                    tamper(self.crisium / f"epoch-{e}")
        set_read_only(self.crisium)

    def run_mode(self, *extra, node=True):
        argv = ["from-checkpoint", "--dir", str(self.tmp / "work"), "--checkpoints", str(self.crisium),
                "--rpc", self.url, "--app", APP, "--native", *map(str, extra)]
        if node:
            argv += ["--node", self.url]
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = cli.main(argv)
        self.out = out.getvalue()
        self.run_json = json.loads((self.tmp / "work" / "run.json").read_text())
        return code

    def failed(self, kind):
        return [c["check"] for c in self.run_json["checks"] if not c["ok"] and c["kind"] == kind]

    def last_call(self):
        return json.loads(self.calls.read_text().splitlines()[-1])

    def test_middle_of_a_sealed_epoch(self):
        self.crisium_stores(2, 3)
        before = {p.name: p.read_bytes() for p in (self.crisium / "epoch-2").iterdir()}
        self.assertEqual(self.run_mode("--to", 4), 0, self.out)
        self.assertEqual(self.run_json["start_epoch"], 2)
        self.assertTrue(self.run_json["start_anchored_on_chain"])
        inputs = self.run_json["inputs"]
        self.assertEqual([i["index"] for i in inputs], [2, 3, 4])
        self.assertEqual([i["hash_after"] for i in inputs], [self.world.states[i][0] for i in (2, 3, 4)])
        self.assertEqual([i["outcome"] for i in inputs], ["rx-accepted", "rx-rejected", "rx-accepted"])
        self.assertEqual(inputs[2]["independent_outputs_root"], root_of(self.world.states[4][1]))
        self.assertEqual(inputs[2]["output_count"], 3 + 2 + 1)
        self.assertEqual(self.failed("setup") + self.failed("divergence"), [])
        self.assertNotIn("epoch 2 end", self.out)  # the epoch is not finished
        call = self.last_call()
        self.assertEqual((call["begin"], call["end"]), (2, 5))
        self.assertEqual(Path(call["load"]), (self.tmp / "work" / "checkpoint").resolve())
        self.assertEqual((call["begin_epoch"], call["next_input_offset"], call["check_outputs_merkle_root"]),
                         ("true", "0", "false"))
        self.assertEqual(sorted(call["flags"]), ["--clear-hash-tree", "--initial-hash"])
        self.assertEqual({p.name: p.read_bytes() for p in (self.crisium / "epoch-2").iterdir()}, before)
        self.assertIn("timings (s):", self.out)

    def test_whole_epoch_against_staged_and_next_seal(self):
        self.crisium_stores(2)
        self.assertEqual(self.run_mode("--epoch", 2), 0, self.out)
        ends = [c for c in self.run_json["checks"] if c["check"].startswith("epoch 2 end")]
        self.assertEqual(len(ends), 4)
        self.assertTrue(all(c["ok"] for c in ends))

    def test_empty_first_epoch(self):
        self.crisium_stores(0)
        self.assertEqual(self.run_mode("--epoch", 0), 0, self.out)
        self.assertEqual(self.run_json["inputs"], [])
        self.assertEqual(self.run_json["final_hash"], fake_machine.TEMPLATE_HASH)

    def test_open_epoch_replays_from_the_epoch_in_dispute(self):
        self.crisium_stores(3, 4)
        self.assertEqual(self.run_mode("--to", 9), 0, self.out)
        self.assertEqual((self.run_json["start_epoch"], self.run_json["start_anchored_on_chain"]), (3, True))
        self.assertEqual([i["index"] for i in self.run_json["inputs"]], [7, 8, 9])
        self.assertEqual(self.run_json["inputs"][-1]["hash_after"], self.world.states[9][0])
        self.assertIn("epoch 3 has no EpochStaged and epoch 4 is not sealed yet", self.out)
        self.assertNotIn("ANCHORED ONLY BY CRISIUM", self.out)

    def test_open_epoch_checkpoint_needs_the_flag(self):
        self.crisium_stores(4)
        with self.assertRaisesRegex(SystemExit, "anchored only by crisium"):
            self.run_mode("--epoch", 4, "--to", 10)

    def test_unanchored_latest(self):
        self.crisium_stores(3, 4)
        self.assertEqual(self.run_mode("--to", 10, "--unanchored-latest"), 0, self.out)
        self.assertEqual((self.run_json["start_epoch"], self.run_json["start_anchored_on_chain"]), (4, False))
        self.assertEqual([i["index"] for i in self.run_json["inputs"]], [9, 10])
        self.assertIn("ANCHORED ONLY BY CRISIUM", self.out)
        self.assertIn("epoch 3 has no EpochStaged yet", self.out)
        names = [c["check"] for c in self.run_json["checks"]]
        self.assertIn("epoch.json final_state_hash == node's hash after input 8", names)
        self.assertFalse(any("EpochSealed" in n for n in names))  # never reported as anchored on chain

    def test_unanchored_latest_against_staged(self):
        h, outs = self.world.start(4)
        self.world.staged[3] = (32, h, root_of(outs))
        self.crisium_stores(4)
        self.assertEqual(self.run_mode("--to", 10, "--unanchored-latest"), 0, self.out)
        self.assertIn("epoch.json final_state_hash == EpochStaged(3) post-epoch state (staged, not accepted)",
                      [c["check"] for c in self.run_json["checks"] if c["ok"]])

    def test_unanchored_latest_disagreeing_with_staged_refuses(self):
        _, outs = self.world.start(4)
        self.world.staged[3] = (32, "0x" + "cc" * 32, root_of(outs))
        self.crisium_stores(4)
        self.assertEqual(self.run_mode("--to", 10, "--unanchored-latest"), cli.SETUP_MISMATCH, self.out)

    def test_writable_crisium_directory_is_refused(self):
        self.crisium_stores(2)
        set_read_only(self.crisium, False)
        with self.assertRaisesRegex(RuntimeError, "writable by tycho-dig"):
            self.run_mode("--to", 3)

    def test_checkpoint_of_another_state_refuses(self):
        def wrong_machine(d):
            state = json.loads((d / "state.json").read_text())
            state["hash"] = fake_machine.TEMPLATE_HASH
            (d / "state.json").write_text(json.dumps(state))

        self.crisium_stores(2, tamper=wrong_machine)
        self.assertEqual(self.run_mode("--to", 3), cli.SETUP_MISMATCH, self.out)
        self.assertIn("loaded machine's state hash == EpochSealed initial state", self.failed("setup"))
        self.assertIn("SETUP MISMATCH", self.out)

    def test_wrong_output_history_refuses(self):
        def extra_output(d):
            tree = load_frontier(d / "outputs-frontier.json")
            tree.append(b"an output that never happened")
            store_frontier(tree, d / "outputs-frontier.json")

        self.crisium_stores(2, tamper=extra_output)
        self.assertEqual(self.run_mode("--to", 3), cli.SETUP_MISMATCH, self.out)
        self.assertTrue(any("EpochSealed outputs root" in c for c in self.failed("setup")))

    def test_machine_declares_a_wrong_root(self):
        epoch2 = list(Chain.EPOCHS[2][1])
        epoch2[2] = b"notices 1 e badroot"
        self.use(Chain(epoch2))
        self.crisium_stores(2)
        self.assertEqual(self.run_mode("--to", 5), cli.MACHINE_WRONG, self.out)
        self.assertEqual(self.run_json["outputs_root_mismatches"], [4])

    def test_staged_end_state_differs(self):
        b, _, root = self.world.staged[2]
        self.world.staged[2] = (b, "0x" + "aa" * 32, root)
        self.crisium_stores(2)
        self.assertEqual(self.run_mode("--epoch", 2), cli.DIVERGENCE, self.out)
        self.assertEqual(self.failed("divergence"), ["epoch 2 end state == EpochStaged(2) post-epoch state"])

    def test_node_diverges(self):
        self.world.node_hashes[3] = "0x" + "bb" * 32
        self.crisium_stores(2)
        self.assertEqual(self.run_mode("--to", 4), cli.DIVERGENCE, self.out)
        self.assertEqual(self.failed("divergence"), ["input 3 machine hash == node"])

    def test_older_emulator_is_refused(self):
        self.crisium_stores(2)
        with mock.patch.dict(os.environ, {"FAKE_EMULATOR": "0.21"}):
            with self.assertRaisesRegex(RuntimeError, "needs emulator 0.23"):
                self.run_mode("--to", 3)


if __name__ == "__main__":
    unittest.main()
