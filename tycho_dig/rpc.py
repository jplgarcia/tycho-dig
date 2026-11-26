"""Minimal JSON-RPC client (standard library only)."""

from __future__ import annotations

import json
import urllib.request
from itertools import count

_ids = count(1)


class RpcError(RuntimeError):
    pass


def call(url: str, method: str, params, timeout: float = 30.0):
    body = json.dumps({"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params})
    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "tycho-dig"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        reply = json.loads(resp.read())
    if "error" in reply and reply["error"]:
        err = reply["error"]
        raise RpcError(f"{method}: {err.get('message', err)}")
    return reply["result"]
