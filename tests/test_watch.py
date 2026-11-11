import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from micronode import cli
from micronode.evm import INPUT_ADDED_TOPIC, EvmAdvance, encode_bytes, encode_evm_advance
from micronode.watch import STATE_FILE, Config, run_watch

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
import fake_machine  # noqa: E402

FAKES = Path(__file__).parent / "fakes"
APP = "0x" + "ab" * 20
INPUTBOX = "0x" + "cd" * 20


class World:
    """A chain with an InputBox and a rollups-node, as far as the micronode sees them."""

    def __init__(self):
        self.finalized = 0
        self.inputs = []  # (block, raw input)
        self.node_hashes = {}  # index -> hash the node recorded (absent: unprocessed)
        self.template_hash = fake_machine.TEMPLATE_HASH
        self.app = {"status": "OK", "enabled": True, "reason": None}

    def add(self, block, payload):
        adv = EvmAdvance(31337, APP, "0x" + "11" * 20, block, 1700000000 + block, 0, len(self.inputs), payload)
        self.inputs.append((block, encode_evm_advance(adv)))

    def true_hashes(self):
        """The hash after every input, by the fake machine's model (what an honest node records)."""
        h, out = fake_machine.TEMPLATE_HASH, []
        for _, raw in self.inputs:
            _, h, _ = fake_machine.model(h, raw)
            out.append(h)
        return out

    def node_catches_up(self, upto=None):
        for i, h in enumerate(self.true_hashes()[: upto]):
            self.node_hashes[i] = h

    def handle(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"number": hex(self.finalized)}
        if method == "eth_call":
            return self.template_hash
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            assert f["topics"][0] == INPUT_ADDED_TOPIC
            return [
                {"blockNumber": hex(b), "logIndex": "0x0", "transactionHash": "0x" + f"{i:064x}",
                 "topics": [INPUT_ADDED_TOPIC, "0x" + APP[2:].rjust(64, "0"), hex(i)],
                 "data": "0x" + ((32).to_bytes(32, "big") + encode_bytes(raw)).hex()}
                for i, (b, raw) in enumerate(self.inputs) if lo <= b <= hi
            ]
        if method == "cartesi_getApplication":
            return {"data": {"iapplication_address": APP, "iinputbox_address": INPUTBOX, "iinputbox_block": "0x1",
                             "template_hash": self.template_hash, **self.app}}
        if method == "cartesi_listInputs":
            items = [{"index": hex(i), "machine_hash": self.node_hashes.get(i)} for i in range(len(self.inputs))]
            return {"data": items[params["offset"]:], "pagination": {"total_count": len(items)}}
        raise AssertionError(method)


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        world = self.world

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

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "bin").mkdir()
        (self.tmp / "template").mkdir()
        exe = self.tmp / "bin" / "cartesi-machine"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "fake_machine.py"}" "$@"\n')
        exe.chmod(0o755)
        self.calls, self.alerts = self.tmp / "calls.jsonl", self.tmp / "alerts.txt"
        env = mock.patch.dict(os.environ, {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}",
                                           "FAKE_CALLS": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config(dir=self.tmp / "state", template=self.tmp / "template", rpc=self.url, node=self.url,
                          app="app", native=True, alert_cmd=f'cat >> "{self.alerts}"; echo >> "{self.alerts}"')

    def tick(self):
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = run_watch(self.cfg, 0, once=True)
        return code, out.getvalue()

    def alert_lines(self):
        return self.alerts.read_text().split("\n") if self.alerts.exists() else []

    def state(self):
        return json.loads((self.cfg.dir / STATE_FILE).read_text())

    def last_call(self):
        return json.loads(self.calls.read_text().splitlines()[-1])

    def test_follows_the_chain_and_resumes(self):
        w = self.world
        w.add(10, b"notices 1")
        w.add(11, b"reject")
        w.finalized = 20
        w.node_catches_up()
        code, out = self.tick()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.last_call(), {"begin": 0, "end": 2, "load": str(self.cfg.template.resolve())})
        w.add(21, b"notices 2")
        w.add(40, b"notices 1")  # not final yet
        w.finalized = 30
        w.node_catches_up()
        code, out = self.tick()
        self.assertEqual(code, 0, out)
        # only the new input, from the machine stored at the end of the previous tick
        self.assertEqual(self.last_call(), {"begin": 2, "end": 3, "load": str((self.cfg.dir / "machine").resolve())})
        st = self.state()
        self.assertEqual([i["hash_after"] for i in st["replayed"]], w.true_hashes()[:3])
        self.assertEqual(st["replayed"][2]["output_count"], 3)
        self.assertFalse(self.alert_lines())
        code, out = self.tick()  # nothing new: no run at all
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_divergence_alerts_once_then_resolves(self):
        w = self.world
        for b in (10, 11, 12):
            w.add(b, b"notices 1")
        w.finalized = 20
        w.node_catches_up()
        w.node_hashes[1] = "0x" + "ee" * 32
        code, out = self.tick()
        self.assertEqual(code, cli.DIVERGENCE, out)
        self.assertIn("ALERT divergence:1: DIVERGENCE at input 1", self.alerts.read_text())
        code, out = self.tick()
        self.assertEqual(self.alerts.read_text().count("ALERT"), 1)  # not sent again
        w.node_catches_up()
        code, out = self.tick()
        self.assertEqual(code, 0, out)
        self.assertIn("RESOLVED divergence:1", self.alerts.read_text())

    def test_node_lagging(self):
        w = self.world
        w.add(10, b"notices 1")
        w.add(11, b"notices 1")
        w.finalized = 30
        w.node_catches_up(1)
        code, out = self.tick()
        self.assertEqual(code, 0, out)  # 19 blocks: within the limit
        w.finalized = 40
        code, out = self.tick()
        self.assertEqual(code, cli.NO_PROGRESS, out)
        self.assertIn("ALERT lag: the node has not processed 1 finalized input(s); input 1 has been final for 29",
                      self.alerts.read_text())

    def test_node_status(self):
        w = self.world
        w.app = {"status": "FAILED", "enabled": True, "reason": "could not store the machine"}
        code, out = self.tick()
        self.assertEqual(code, cli.NO_PROGRESS, out)
        self.assertIn("ALERT status: app status is FAILED: could not store the machine", self.alerts.read_text())

    def test_machine_wrong(self):
        w = self.world
        w.add(10, b"notices 1")
        w.add(11, b"notices 2 badroot")
        w.finalized = 20
        w.node_catches_up()
        code, out = self.tick()
        self.assertEqual(code, cli.MACHINE_WRONG, out)
        self.assertIn("ALERT machine-wrong:1: OUTPUTS ROOT MISMATCH (machine wrong): input 1: the machine declared 0x5a5a",
                      self.alerts.read_text())

    def test_setup_mismatch_compares_nothing(self):
        w = self.world
        w.add(10, b"notices 1")
        w.finalized = 20
        w.template_hash = "0x" + "77" * 32
        w.node_hashes[0] = "0x" + "ee" * 32
        code, out = self.tick()
        self.assertEqual(code, cli.SETUP_MISMATCH, out)
        self.assertIn("ALERT setup: SETUP MISMATCH", self.alerts.read_text())
        self.assertNotIn("divergence", self.alerts.read_text())
        self.assertEqual(self.state()["replayed"], [])

    def test_exception_stops_the_machine(self):
        w = self.world
        w.add(10, b"notices 1")
        w.add(11, b"exception")
        w.finalized = 20
        w.node_catches_up()
        code, out = self.tick()
        self.assertIn("ALERT blocked: input 1 raised an exception", self.alerts.read_text())
        w.add(21, b"notices 1")
        w.finalized = 30
        self.tick()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)  # nothing more to advance

    def test_watcher_cannot_check(self):
        self.cfg.node = "http://127.0.0.1:9"  # nothing listens
        for _ in range(2):
            self.tick()
        self.assertFalse(self.alert_lines())
        code, out = self.tick()
        self.assertIn("ALERT watcher: the micronode could not check for 3 ticks", self.alerts.read_text())

    def test_finalized_input_changed(self):
        w = self.world
        w.add(10, b"notices 1")
        w.finalized = 20
        w.node_catches_up()
        self.tick()
        self.cfg_state = self.state()
        # rewind our cursor so the same block is read again, and change the input under it
        st = self.state()
        st["scan_from"] = 5
        (self.cfg.dir / STATE_FILE).write_text(json.dumps(st))
        w.inputs[0] = (10, w.inputs[0][1] + b"x")
        code, out = self.tick()
        self.assertIn("ALERT input-changed:0: finalized input 0 changed", self.alerts.read_text())


if __name__ == "__main__":
    unittest.main()
