import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
from pathlib import Path

from micronode import cli
from micronode.evm import EvmAdvance, decode_abi_bytes, decode_evm_advance, encode_bytes, encode_evm_advance
from micronode.machine import parse_run_log

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


# Machine hash after each input, as the rollups-node a13 recorded it for the tester app on a
# live stack (cartesi_listInputs, 2026-11-05). The micronode run below matched all eight.
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
        from micronode.machine import _cli_args

        self.assertIn("input_file_index_begin:0", _cli_args(0, 3, "/t", True)[3])
        self.assertIn("input_index_end:3", _cli_args(0, 3, "/t", True, legacy=True)[3])


class FakeNode(BaseHTTPRequestHandler):
    hashes: list = []
    app: dict = {}

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        params = req["params"]
        if req["method"] == "cartesi_getApplication":
            result = {"data": {"status": "OK", "enabled": True, "reason": None, **self.app}}
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


class CompareTest(unittest.TestCase):
    def _compare(self, node_hashes, app=None, manifest=None):
        FakeNode.hashes = node_hashes
        FakeNode.app = app or {}
        server = HTTPServer(("127.0.0.1", 0), FakeNode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run = {"inputs": [{"index": i, "hash_after": h} for i, h in enumerate(AFTER)]}
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

    def test_agree(self):
        code, out = self._compare(AFTER)
        self.assertEqual(code, 0, out)

    def test_first_divergence(self):
        bad = AFTER[:1] + ["0x" + "ab" * 32, "0x" + "cd" * 32]
        code, out = self._compare(bad)
        self.assertEqual(code, 2)
        self.assertIn("first at input 1", out)

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


if __name__ == "__main__":
    unittest.main()
