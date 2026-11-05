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


class FakeNode(BaseHTTPRequestHandler):
    hashes: list = []

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        params = req["params"]
        items = [{"index": hex(i), "machine_hash": h} for i, h in enumerate(self.hashes)]
        page = items[params["offset"] : params["offset"] + params["limit"]]
        body = json.dumps(
            {"jsonrpc": "2.0", "id": req["id"],
             "result": {"data": page, "pagination": {"total_count": len(items), "limit": params["limit"], "offset": params["offset"]}}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CompareTest(unittest.TestCase):
    def _compare(self, node_hashes):
        FakeNode.hashes = node_hashes
        server = HTTPServer(("127.0.0.1", 0), FakeNode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run = {"inputs": [{"index": i, "hash_after": h} for i, h in enumerate(AFTER)]}
                Path(tmp, cli.RUN_FILE).write_text(json.dumps(run))
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

    def test_node_behind(self):
        code, out = self._compare(AFTER[:2] + [None])
        self.assertEqual(code, 0, out)
        self.assertIn("1 not yet processed", out)


if __name__ == "__main__":
    unittest.main()
