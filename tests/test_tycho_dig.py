import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

from tycho_dig import cli
from tycho_dig.evm import EvmAdvance, decode_abi_bytes, decode_evm_advance, encode_bytes, encode_evm_advance
from tycho_dig.machine import parse_run_log

FIXTURES = Path(__file__).parent / "fixtures"

# cast abi-encode "f(uint256,address,address,uint256,uint256,uint256,uint256,bytes)" \
#   31337 0x1111...1111 0x2222...2222 100 1700000000 0 0 0x68656c6c6f   (selector 0x415bf363 prepended)
CAST_HELLO = (
    "415bf363"
    "0000000000000000000000000000000000000000000000000000000000007a69"
    "0000000000000000000000001111111111111111111111111111111111111111"
    "0000000000000000000000002222222222222222222222222222222222222222"
    "0000000000000000000000000000000000000000000000000000000000000064"
    "000000000000000000000000000000000000000000000000000000006553f100"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000100"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "68656c6c6f000000000000000000000000000000000000000000000000000000"
)

HELLO = EvmAdvance(
    chain_id=31337,
    app_contract="0x" + "11" * 20,
    msg_sender="0x" + "22" * 20,
    block_number=100,
    block_timestamp=1700000000,
    prev_randao=0,
    index=0,
    payload=b"hello",
)

AFTER = [
    "0x9e84d706fe69b465aaf0c0e8ab77ffcfff40d18b41377adb036678ae2609c83a",
    "0x82f632d93097b4b7aa49716962cbd2839b287f5c9428903ff56eac1847930164",
    "0xee31c1fe65b347792f829386dde5941a5d4f04aaafa91845f69007406d9a644d",
]


class EvmTest(unittest.TestCase):
    def test_encoding_matches_cast(self):
        self.assertEqual(encode_evm_advance(HELLO).hex(), CAST_HELLO)

    def test_roundtrip(self):
        self.assertEqual(decode_evm_advance(bytes.fromhex(CAST_HELLO)), HELLO)

    def test_log_bytes(self):
        data = (32).to_bytes(32, "big") + encode_bytes(b"\x01\x02\x03")
        self.assertEqual(decode_abi_bytes(data), b"\x01\x02\x03")


class ParseTest(unittest.TestCase):
    def test_three_inputs(self):
        result = parse_run_log((FIXTURES / "run-3-inputs.log").read_text())
        self.assertEqual([i.index for i in result.inputs], [0, 1, 2])
        self.assertEqual([i.hash_after for i in result.inputs], AFTER)
        self.assertEqual({i.outcome for i in result.inputs}, {"rx-accepted"})
        self.assertEqual(result.final_hash, AFTER[2])
        # the state after input N is the boundary state before input N+1
        self.assertEqual(result.inputs[1].hash_before, AFTER[0])


# Machine hash after each input, as rollups-node v2.0.0-alpha.13 recorded it for the tester app on a
# live stack (cartesi_listInputs, 2026-11-05). tycho-dig run below matched all eight.
TESTER_NODE = [
    ("rx-accepted", "0x660d8a75f21c7c9cf9a4fa94a704595b49c9306cdb2b389ad27fc505472a85d7"),
    ("rx-rejected", "0x660d8a75f21c7c9cf9a4fa94a704595b49c9306cdb2b389ad27fc505472a85d7"),
    ("rx-accepted", "0xb6493d23f666ca3752a57b77c30721207ce986f55738626c158f025a934fc64f"),
    ("rx-accepted", "0x64cbc97dacc9788d839dc70d3b4a7b34c89658b267fb8ace9d8340d4690b4a75"),
    ("rx-accepted", "0xcf6316a5f50112c393fe208aa1b165e8e5c2f02c55f78db181f089b2ecf936c4"),
    ("rx-rejected", "0xcf6316a5f50112c393fe208aa1b165e8e5c2f02c55f78db181f089b2ecf936c4"),
    ("rx-accepted", "0xaffaec366e8c750d54462076e6b3e37d22dadccb79433ddbcc85631c2f48434b"),
    ("tx-exception", "0x50932078906dd63d320d48538e9edf13c39f8bafe292bdbcaab2844f513df2b2"),
]


class TesterLogTest(unittest.TestCase):
    """emulator 0.21 log with rejected inputs and a final exception."""

    def test_matches_node(self):
        result = parse_run_log((FIXTURES / "run-tester-8-inputs.log").read_text())
        self.assertEqual([(i.outcome, i.hash_after) for i in result.inputs], TESTER_NODE)

    def test_rejected_input_reverts(self):
        result = parse_run_log((FIXTURES / "run-tester-8-inputs.log").read_text())
        self.assertEqual(result.inputs[1].hash_after, result.inputs[1].hash_before)


class CliArgsTest(unittest.TestCase):
    def test_range_keys_by_version(self):
        from tycho_dig.machine import _cli_args

        self.assertIn("input_file_index_begin:0", _cli_args(0, 3, "/t", True)[3])
        self.assertIn("input_index_end:3", _cli_args(0, 3, "/t", True, legacy=True)[3])


class FakeNode(BaseHTTPRequestHandler):
    hashes: list = []
    app: dict = {}
    epochs: dict = {}

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        params = req["params"]
        if req["method"] == "cartesi_getApplication":
            result = {"data": {"status": "OK", "enabled": True, "reason": None, **self.app}}
        elif req["method"] == "cartesi_getEpoch":
            result = {"data": self.epochs[int(params["epoch_index"], 16)]}
        else:
            items = [{"index": hex(i), "machine_hash": h} for i, h in enumerate(self.hashes)]
            page = items[params["offset"] : params["offset"] + params["limit"]]
            result = {"data": page, "pagination": {"total_count": len(items), "limit": params["limit"], "offset": params["offset"]}}
        body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class NodeTestBase(unittest.TestCase):
    def _compare(self, node_hashes, app=None, manifest=None, initial_hash=None, inputs=None, epochs=None):
        FakeNode.hashes = node_hashes
        FakeNode.app = app or {}
        FakeNode.epochs = epochs or {}
        server = HTTPServer(("127.0.0.1", 0), FakeNode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run = {
                    "initial_hash": initial_hash,
                    "emulator_version": "0.23.0-test2",
                    "inputs": inputs or [{"index": i, "hash_after": h} for i, h in enumerate(AFTER)],
                }
                Path(tmp, cli.RUN_FILE).write_text(json.dumps(run))
                if manifest is not None:
                    Path(tmp, cli.FETCH_FILE).write_text(json.dumps(manifest))
                out = StringIO()
                with redirect_stdout(out):
                    code = cli.do_compare(f"http://127.0.0.1:{server.server_port}", "app", Path(tmp))
                return code, out.getvalue()
        finally:
            server.shutdown()
            server.server_close()


class CompareTest(NodeTestBase):
    def test_agree(self):
        code, out = self._compare(AFTER)
        self.assertEqual(code, 0, out)

    def test_first_divergence(self):
        bad = AFTER[:1] + ["0x" + "ab" * 32, "0x" + "cd" * 32]
        code, out = self._compare(bad)
        self.assertEqual(code, 2)
        self.assertIn("first at input 1", out)

    def test_template_matches(self):
        tpl = "0x" + "11" * 32
        code, out = self._compare(AFTER, app={"template_hash": tpl}, initial_hash=tpl)
        self.assertEqual(code, 0, out)
        self.assertIn("template matches", out)

    def test_template_mismatch_refuses(self):
        # e.g. a tycho-dig on 0.23 (new MIMPID) against an app deployed with a 0.22 template:
        # every hash differs, so it must not be reported as a divergence
        code, out = self._compare(
            ["0x" + "cd" * 32] * 3, app={"template_hash": "0x" + "11" * 32}, initial_hash="0x" + "22" * 32
        )
        self.assertEqual(code, cli.SETUP_MISMATCH, out)
        self.assertIn("SETUP MISMATCH", out)
        self.assertIn("0.23.0-test2", out)
        self.assertNotIn("DIVERGENCE", out)

    def test_app_failed(self):
        code, out = self._compare(AFTER[:2] + [None], app={"status": "FAILED", "reason": "could not store\nthe machine"})
        self.assertEqual(code, 3, out)
        self.assertIn("PROGRESS ALARM: app status is FAILED: could not store | the machine", out)

    def test_app_disabled(self):
        code, out = self._compare(AFTER, app={"enabled": False})
        self.assertEqual(code, 3, out)
        self.assertIn("disabled", out)

    def test_divergence_wins_over_progress(self):
        code, out = self._compare(["0x" + "ab" * 32] + AFTER[1:], app={"status": "DIVERGED"})
        self.assertEqual(code, 2)
        self.assertIn("PROGRESS ALARM: app status is DIVERGED", out)

    def _manifest(self, to_block):
        return {"to_block": to_block, "inputs": [{"index": i, "block_number": 100 + i} for i in range(3)]}

    def test_lag_over_limit(self):
        # input 2 sits in block 102; the finalized head is 130: 28 blocks > 25
        code, out = self._compare(AFTER[:2] + [None], manifest=self._manifest(130))
        self.assertEqual(code, 3, out)
        self.assertIn("input 2 has been final for 28 blocks", out)

    def test_lag_within_limit(self):
        code, out = self._compare(AFTER[:2] + [None], manifest=self._manifest(120))
        self.assertEqual(code, 0, out)

    def test_node_behind(self):
        code, out = self._compare(AFTER[:2] + [None])
        self.assertEqual(code, 0, out)
        self.assertIn("1 not yet processed", out)


# --- A guest that declares a wrong outputs root: input 1 accepted with 0x5a..5a as its root -------
# Evidence of the outputs-root mismatch run of 2026-11-09 (outgen guest, emulator 0.21.0 in the rollups-node
# v2.0.0-alpha.13 image): the outputs the machine emitted, the roots it declared, and the node's reason.

BAD_ROOT_RUN = FIXTURES / "bad-root"
FAKES = Path(__file__).parent / "fakes"
GOOD_ROOT_0 = "0x8b900f66313f1504e76e48ee76b885b87701275f8d79d3e49fdaf261f7328f8e"
GOOD_ROOT_1 = "0xb2b72954d732ed70e622f4b388620239b2a97df685855b6c3c29588bcdcac2e6"
BAD_ROOT = "0x" + "5a" * 32
BUGGY_NODE_ROOT = "0xaafeabd336716faab6f77788935ed1fcd81a22a6a0196d132cafc1ef231244d8"  # last output dropped
BAD_ROOT_RUN_OUTPUTS = ["out-0-0.bin", "out-1-1.bin", "out-1-2.bin"]


class OutputsRootTest(unittest.TestCase):
    def test_keccak_implementations_agree(self):
        from tycho_dig import outputs

        for data in (b"", b"abc", bytes(range(256)) * 3):
            self.assertEqual(outputs.keccak256(data), outputs.keccak256_py(data))

    def test_bad_root_run_roots(self):
        from tycho_dig.outputs import outputs_root

        blobs = [(BAD_ROOT_RUN / "machine-wrong" / n).read_bytes() for n in BAD_ROOT_RUN_OUTPUTS]
        self.assertEqual("0x" + outputs_root(blobs[:1]).hex(), GOOD_ROOT_0)
        self.assertEqual("0x" + outputs_root(blobs).hex(), GOOD_ROOT_1)
        self.assertEqual("0x" + (BAD_ROOT_RUN / "machine-wrong" / "root-1.bin").read_bytes().hex(), BAD_ROOT)
        self.assertEqual("0x" + (BAD_ROOT_RUN / "control" / "root-1.bin").read_bytes().hex(), GOOD_ROOT_1)

    def test_real_cli_run(self):
        # tycho-dig run on the bad-root run's inputs with the real 0.21.0 CLI (rollups-node v2.0.0-alpha.13 image), root
        # check off; the machine hashes are the ones the node recorded for app "root"
        from tycho_dig.machine import verify_outputs_roots

        result = parse_run_log((BAD_ROOT_RUN / "machine-wrong" / "run-check-off.log").read_text())
        for item in result.inputs:
            item.outputs_merkle_root = "0x" + (BAD_ROOT_RUN / "machine-wrong" / f"root-{item.index}.bin").read_bytes().hex()
        verify_outputs_roots(result, BAD_ROOT_RUN / "machine-wrong")
        self.assertEqual(result.initial_hash, "0xbc2ff7bd4e6f46db9525a517e3f1dcb171e4da93e69dd777423086e1a486c85f")
        self.assertEqual(
            [i.hash_after for i in result.inputs],
            ["0xd695f7a53a7e61d245991373a97b2649258fc7e5765a2177797ad39fcf210348",
             "0xf1c56901eb869a701d74345ccb38c7fbef2bee3b4d15173f04161870e988290c"],
        )
        self.assertEqual([i.index for i in result.outputs_root_mismatches], [1])
        self.assertEqual(result.inputs[1].independent_outputs_root, GOOD_ROOT_1)
        self.assertIn("Shutdown JSONRPC remote cartesi machine", result.log)

    def test_cli_assertion_is_parsed(self):
        result = parse_run_log((BAD_ROOT_RUN / "machine-wrong" / "cli-check-true-tail.log").read_text())
        self.assertEqual(result.cli_root_check_stop, 1)


class FakeCliRun(unittest.TestCase):
    """machine.run against a fake CLI that replays the bad-root run's files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.bin, self.work = tmp / "bin", tmp / "work"
        self.bin.mkdir()
        self.work.mkdir()
        for name in ("input-0.bin", "input-1.bin"):
            (self.work / name).write_bytes((BAD_ROOT_RUN / "machine-wrong" / name).read_bytes())
        for tool, mode in (("cartesi-machine", "cli"), ("docker", "docker")):
            path = self.bin / tool
            path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / "fake_cli.py"}" {mode} "$@"\n')
            path.chmod(0o755)
        self.pidfile, self.docker_log = tmp / "server.pid", tmp / "docker.log"
        self.env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_OUTPUTS": str(BAD_ROOT_RUN / "machine-wrong"),
            "FAKE_ROOTS": str(BAD_ROOT_RUN / "machine-wrong"),
            "FAKE_CAPS": "check_option,remote_shutdown",
            "FAKE_PIDFILE": str(self.pidfile),
            "FAKE_DOCKER_LOG": str(self.docker_log),
        }

    def _run(self, native=True, **env):
        from tycho_dig import machine

        with mock.patch.dict(os.environ, {**self.env, **env}):
            result = machine.run(Path(self.tmp.name), self.work, 0, 2, image="fake", native=native)
            machine.verify_outputs_roots(result, self.work)
            return result

    def _cmd_run(self, **env):
        out = StringIO()
        with mock.patch.dict(os.environ, {**self.env, **env}), redirect_stdout(out):
            code = cli.main(["run", "--dir", str(self.work), "--template", self.tmp.name, "--native"])
        return code, out.getvalue()

    def assert_server_gone(self):
        pid = int(self.pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        self.fail(f"the remote machine server {pid} outlived the run")

    def test_machine_wrong_is_a_detection(self):
        result = self._run()
        # the CLI's check was off, so the replay went past the bad input to the final hash
        self.assertIsNone(result.cli_root_check_stop)
        self.assertEqual(result.final_hash, "0x" + "09" * 32)
        self.assertEqual([i.index for i in result.outputs_root_mismatches], [1])
        bad = result.inputs[1]
        self.assertEqual((bad.outputs_merkle_root, bad.independent_outputs_root, bad.output_count), (BAD_ROOT, GOOD_ROOT_1, 3))
        self.assertEqual(result.inputs[0].independent_outputs_root, GOOD_ROOT_0)
        self.assertFalse(result.inputs[0].outputs_root_mismatch)
        self.assertIn("Shutdown JSONRPC remote cartesi machine", result.log)
        self.assert_server_gone()

    def test_run_exit_code(self):
        code, out = self._cmd_run()
        self.assertEqual(code, cli.MACHINE_WRONG, out)
        self.assertIn(f"OUTPUTS ROOT MISMATCH (machine wrong): input 1: the machine declared {BAD_ROOT}", out)
        self.assertIn(f"its own 3 outputs since genesis give {GOOD_ROOT_1}", out)
        run = json.loads((self.work / cli.RUN_FILE).read_text())
        self.assertEqual(run["outputs_root_mismatches"], [1])

    def test_control_is_clean(self):
        code, out = self._cmd_run(FAKE_ROOTS=str(BAD_ROOT_RUN / "control"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("MISMATCH", out)

    def test_cli_that_cannot_skip_its_check(self):
        # the CLI's own assertion stops the run with exit 1: still a detection, not a crash
        result = self._run(FAKE_CAPS="")
        self.assertEqual(result.cli_root_check_stop, 1)
        self.assertEqual([i.index for i in result.outputs_root_mismatches], [1])
        self.assertIn("Left alive", result.log)
        self.assert_server_gone()  # killed with the CLI's process group

    def test_other_failures_still_raise_and_clean_up(self):
        with self.assertRaises(RuntimeError):
            self._run(FAKE_CRASH="1", FAKE_CAPS="")
        self.assert_server_gone()

    def test_docker_container_removed(self):
        result = self._run(native=False, FAKE_CAPS="check_option")
        self.assertEqual([i.index for i in result.outputs_root_mismatches], [1])
        self.assertRegex(self.docker_log.read_text(), r"^rm -f tycho-dig-[0-9a-f]{12}\n$")
        self.assert_server_gone()

    def test_stale_files_are_removed(self):
        (self.work / "out-5-7.bin").write_bytes(b"stale")
        self._run()
        self.assertFalse((self.work / "out-5-7.bin").exists())


def _bad_root_run_inputs(root_1):
    return [
        {"index": 0, "hash_after": AFTER[0], "outcome": "rx-accepted", "outputs_merkle_root": GOOD_ROOT_0,
         "independent_outputs_root": GOOD_ROOT_0, "output_count": 1},
        {"index": 1, "hash_after": AFTER[1], "outcome": "rx-accepted", "outputs_merkle_root": root_1,
         "independent_outputs_root": GOOD_ROOT_1, "output_count": 3},
    ]


BAD_ROOT_RUN_EPOCHS = {1: {"index": "0x1", "input_index_lower_bound": "0x0", "input_index_upper_bound": "0x2"}}


class OutputsRootCrossCheckTest(NodeTestBase):
    """Diego's cross-check against the node's INVALID_OUTPUTS_ROOT reason."""

    def _node_app(self, declared, calculated):
        app = json.loads((BAD_ROOT_RUN / "node-getApplication.json").read_text())["result"]["data"]
        reason = app["reason"]
        app["reason"] = reason.replace(BAD_ROOT, declared).replace(GOOD_ROOT_1, calculated)
        return app

    def test_machine_wrong_live_evidence(self):
        # the node's own reason from the bad-root run: declared 0x5a.., calculated 0xb2b7..
        code, out = self._compare(AFTER[:2], app=self._node_app(BAD_ROOT, GOOD_ROOT_1),
                                  inputs=_bad_root_run_inputs(BAD_ROOT), epochs=BAD_ROOT_RUN_EPOCHS)
        self.assertEqual(code, cli.MACHINE_WRONG, out)
        self.assertIn("VERDICT: MACHINE WRONG", out)
        self.assertIn("OUTPUTS ROOT MISMATCH (machine wrong): input 1", out)
        self.assertIn("PROGRESS ALARM: app status is INVALID_OUTPUTS_ROOT", out)

    def test_node_wrong_simulated(self):
        # the machine's root is right; a node that dropped the last output calculates another
        code, out = self._compare(AFTER[:2], app=self._node_app(GOOD_ROOT_1, BUGGY_NODE_ROOT),
                                  inputs=_bad_root_run_inputs(GOOD_ROOT_1), epochs=BAD_ROOT_RUN_EPOCHS)
        self.assertEqual(code, cli.NODE_WRONG, out)
        self.assertIn("VERDICT: NODE WRONG", out)
        self.assertNotIn("machine wrong", out)

    def test_machine_wrong_without_node_flag(self):
        # the node has not closed the epoch yet: tycho-dig alone already knows
        code, out = self._compare(AFTER[:2], inputs=_bad_root_run_inputs(BAD_ROOT))
        self.assertEqual(code, cli.MACHINE_WRONG, out)
        self.assertNotIn("VERDICT", out)

    def test_cannot_decide_before_replaying_the_epoch(self):
        code, out = self._compare(AFTER[:1], app=self._node_app(BAD_ROOT, GOOD_ROOT_1),
                                  inputs=_bad_root_run_inputs(GOOD_ROOT_1)[:1], epochs=BAD_ROOT_RUN_EPOCHS)
        self.assertIn("cannot cross-check epoch 1 yet", out)
        self.assertEqual(code, 3, out)


if __name__ == "__main__":
    unittest.main()
