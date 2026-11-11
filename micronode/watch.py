"""micronode watch: follow an application and raise alerts when the node and the micronode disagree.

Every tick:

1. reads the new InputAdded events from the InputBox, up to the finalized block, through
   an Ethereum RPC of our choosing (not the node's);
2. advances the micronode's machine through the new inputs with the cartesi-machine CLI,
   resuming from the machine it stored at the end of the previous tick;
3. compares the machine hash after every input with the node's (cartesi_listInputs), and
   checks the outputs roots, the node's status and its progress;
4. sends an alert for every alarm that was not raised before, and a "resolved" note for
   every alarm that went away.

The state lives in one directory, so the watcher can be stopped and started again: inputs,
per-input results, the outputs tree, the stored machine and the alarms already sent.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import chain, machine
from .outputs import OutputsTree, keccak256

STATE_FILE = "watch.json"
MACHINE_DIR = "machine"  # the machine after the last replayed input
FAILURES_BEFORE_ALERT = 3


@dataclass
class Config:
    dir: Path
    template: Path
    rpc: str
    node: str
    app: str  # as the node knows it (name or address)
    inputbox: str | None = None
    from_block: int | None = None
    to_block: str = "finalized"
    image: str = machine.DEFAULT_IMAGE
    native: bool = False
    max_lag_blocks: int | None = 25
    alert_cmd: str | None = None
    alert_webhook: str | None = None
    label: str | None = None  # how alerts name the app; defaults to --app


@dataclass
class State:
    app_address: str | None = None
    inputbox: str | None = None
    scan_from: int | None = None  # next block to read InputAdded from
    finalized: int | None = None  # last block read
    template_hash: str | None = None  # getTemplateHash() of the application contract
    emulator_version: str | None = None
    inputs: list[dict] = field(default_factory=list)  # fetched: index, block_number, transaction_hash, keccak
    replayed: list[dict] = field(default_factory=list)  # per-input results, in index order
    tree: dict | None = None  # outputs tree after the last replayed input
    last_hash: str | None = None  # state hash of the stored machine
    blocked: str | None = None  # why the micronode stopped advancing (setup mismatch, exception)
    raised: dict[str, str] = field(default_factory=dict)  # alarm key -> message already sent
    failures: int = 0  # consecutive ticks that failed
    last_error: str | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1))
        tmp.replace(path)


# --- the three steps ---------------------------------------------------------------------------


def _resolve(cfg: Config, st: State) -> None:
    if st.app_address:
        return
    app, inputbox, from_block = cfg.app, cfg.inputbox, cfg.from_block
    if inputbox is None or from_block is None or not app.startswith("0x"):
        info = chain.node_application(cfg.node, app)
        app = info["iapplication_address"]
        inputbox = inputbox or info["iinputbox_address"]
        if from_block is None:
            block = info.get("iinputbox_block", 0)
            from_block = int(block, 16) if isinstance(block, str) else int(block)
    st.app_address, st.inputbox, st.scan_from = app, inputbox, from_block or 0
    st.template_hash = chain.onchain_template_hash(cfg.rpc, app)


def fetch(cfg: Config, st: State, alarms: dict[str, str]) -> None:
    """Read new finalized inputs; check that the ones already read did not change."""
    head = chain.resolve_block(cfg.rpc, cfg.to_block)
    if st.finalized is not None and head < st.finalized:
        alarms["chain-rewind"] = f"the {cfg.to_block} block went back from {st.finalized} to {head}"
        return
    if head >= st.scan_from:
        for item in chain.fetch_inputs(cfg.rpc, st.inputbox, st.app_address, st.scan_from, head):
            digest = "0x" + keccak256(item.raw).hex()
            if item.index < len(st.inputs):
                if st.inputs[item.index]["keccak"] != digest:
                    alarms[f"input-changed:{item.index}"] = (
                        f"finalized input {item.index} changed: was {st.inputs[item.index]['keccak']}, "
                        f"now {digest} (tx {item.transaction_hash})"
                    )
                continue
            if item.index != len(st.inputs):
                raise RuntimeError(f"InputBox gap: got input {item.index}, expected {len(st.inputs)}")
            (cfg.dir / f"input-{item.index}.bin").write_bytes(item.raw)
            st.inputs.append(
                {"index": item.index, "block_number": item.block_number,
                 "transaction_hash": item.transaction_hash, "keccak": digest}
            )
        st.scan_from = head + 1
    st.finalized = head


def advance(cfg: Config, st: State, alarms: dict[str, str]) -> None:
    """Advance the machine through the inputs fetched but not replayed yet."""
    begin, end = len(st.replayed), len(st.inputs)
    if st.blocked or end <= begin:
        return
    stored = cfg.dir / MACHINE_DIR
    new = cfg.dir / (MACHINE_DIR + ".new")
    shutil.rmtree(new, ignore_errors=True)
    load = stored if begin > 0 else cfg.template
    result = machine.run(load, cfg.dir, begin, end, image=cfg.image, native=cfg.native,
                         store=new, need_root_check_option=True)
    st.emulator_version = result.emulator_version
    if not result.inputs or result.inputs[0].index != begin:
        raise RuntimeError(f"the CLI did not start at input {begin}:\n{result.log[-2000:]}")
    first = result.inputs[0].hash_before
    if begin == 0:
        if st.template_hash and first != st.template_hash:
            st.blocked = (
                f"SETUP MISMATCH: the micronode's template state hash {first} is not the application's "
                f"{st.template_hash} (emulator {result.emulator_version}): another emulator version or "
                f"template. Nothing compared; restart with the application's template and version."
            )
            shutil.rmtree(new, ignore_errors=True)
            return
    elif first != st.last_hash:
        raise RuntimeError(f"the stored machine resumed at {first}, but it was stored at {st.last_hash}")
    tree = OutputsTree.from_json(st.tree) if st.tree else OutputsTree()
    machine.verify_outputs_roots(result, cfg.dir, tree)
    last = result.inputs[-1]
    if last.outcome == "tx-exception":
        st.blocked = f"input {last.index} raised an exception: the machine cannot take more inputs"
    elif len(result.inputs) != end - begin:
        raise RuntimeError(f"the CLI stopped after input {last.index} (asked for {begin}..{end - 1})")
    elif not (new / "config.json").exists():
        raise RuntimeError(f"the CLI did not store the machine in {new}")
    st.replayed += [asdict(i) for i in result.inputs]
    st.tree = tree.to_json()
    st.last_hash = result.final_hash
    if not st.blocked:
        shutil.rmtree(stored, ignore_errors=True)
        new.replace(stored)


def compare(cfg: Config, st: State, alarms: dict[str, str]) -> None:
    """Every alarm from the node's view against ours, keyed so each is sent once."""
    from .cli import MACHINE_WRONG, NODE_WRONG, machine_wrong_alarms, node_root_crosscheck, status_alarms

    if st.blocked and st.blocked.startswith("SETUP MISMATCH"):
        alarms["setup"] = st.blocked
    info = chain.node_application(cfg.node, cfg.app)
    for alarm in status_alarms(cfg.node, cfg.app, info):
        alarms["disabled" if "disabled" in alarm else "status"] = alarm
    node_template = (info.get("template_hash") or "").lower()
    if st.template_hash and node_template and node_template != st.template_hash:
        alarms["node-template"] = f"the node's template hash {node_template} is not the on-chain {st.template_hash}"
    theirs = chain.node_input_hashes(cfg.node, cfg.app)
    for item in st.replayed:
        node_hash = theirs.get(item["index"])
        if node_hash is not None and node_hash != (item["hash_after"] or "").lower():
            alarms[f"divergence:{item['index']}"] = (
                f"DIVERGENCE at input {item['index']}: micronode {item['hash_after']}, node {node_hash}"
            )
            break  # every later input diverges too; the first one is what matters
    for item in st.replayed:
        for text in machine_wrong_alarms([item]):
            alarms[f"machine-wrong:{item['index']}"] = f"OUTPUTS ROOT MISMATCH (machine wrong): {text}"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        verdict = node_root_crosscheck(cfg.node, cfg.app, info, st.replayed)
    if verdict in (MACHINE_WRONG, NODE_WRONG):
        alarms["outputs-root-verdict"] = out.getvalue().strip()
    if cfg.max_lag_blocks is not None and st.finalized is not None:
        late = [(i["index"], st.finalized - i["block_number"]) for i in st.inputs
                if theirs.get(i["index"]) is None and st.finalized - i["block_number"] > cfg.max_lag_blocks]
        if late:
            alarms["lag"] = (
                f"the node has not processed {len(late)} finalized input(s); input {late[0][0]} has been "
                f"final for {late[0][1]} blocks (limit {cfg.max_lag_blocks})"
            )
    if st.blocked and not st.blocked.startswith("SETUP MISMATCH"):
        alarms["blocked"] = st.blocked


# --- alerts --------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notify(cfg: Config, kind: str, key: str, message: str) -> None:
    """Print the alert and send it through --alert-cmd and --alert-webhook."""
    label = cfg.label or cfg.app
    text = f"[micronode {label}] {kind} {key}: {message}"
    print(f"{_now()} {kind} {key}: {message}", flush=True)
    if cfg.alert_cmd:
        env = {**os.environ, "MICRONODE_ALERT_KIND": kind, "MICRONODE_ALERT_KEY": key,
               "MICRONODE_ALERT_APP": label, "MICRONODE_ALERT_TEXT": text}
        try:
            subprocess.run(cfg.alert_cmd, shell=True, input=text, text=True, env=env, timeout=60)
        except Exception as exc:  # an alert channel failing must not stop the watcher
            print(f"{_now()} warning: --alert-cmd failed: {exc}", file=sys.stderr, flush=True)
    if cfg.alert_webhook:
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(cfg.alert_webhook, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception as exc:
            print(f"{_now()} warning: --alert-webhook failed: {exc}", file=sys.stderr, flush=True)


def publish(cfg: Config, st: State, alarms: dict[str, str]) -> None:
    for key, message in alarms.items():
        if st.raised.get(key) is None:
            notify(cfg, "ALERT", key, message)
    for key in list(st.raised):
        if key not in alarms:
            notify(cfg, "RESOLVED", key, st.raised[key])
    st.raised = dict(alarms)


# --- the loop ------------------------------------------------------------------------------------


def tick(cfg: Config, st: State) -> dict[str, str]:
    """One round: fetch, advance, compare, alert. Returns the alarms now standing."""
    alarms: dict[str, str] = {}
    try:
        _resolve(cfg, st)
        fetch(cfg, st, alarms)
        advance(cfg, st, alarms)
        compare(cfg, st, alarms)
        st.failures, st.last_error = 0, None
    except Exception as exc:
        st.failures += 1
        st.last_error = f"{type(exc).__name__}: {exc}"
        print(f"{_now()} tick failed ({st.failures} in a row): {st.last_error}", file=sys.stderr, flush=True)
        if os.environ.get("MICRONODE_DEBUG"):
            traceback.print_exc()
        # what was standing still stands: a failed tick does not resolve anything
        alarms = {k: v for k, v in st.raised.items() if k != "watcher"}
        if st.failures >= FAILURES_BEFORE_ALERT:
            alarms["watcher"] = f"the micronode could not check for {st.failures} ticks: {st.last_error}"
    publish(cfg, st, alarms)
    return alarms


def exit_code(alarms: dict[str, str]) -> int:
    from .cli import DIVERGENCE, MACHINE_WRONG, NO_PROGRESS, NODE_WRONG, SETUP_MISMATCH

    keys = list(alarms)
    verdict = alarms.get("outputs-root-verdict", "")
    if any(k.startswith("machine-wrong") for k in keys) or "MACHINE WRONG" in verdict:
        return MACHINE_WRONG
    if verdict:  # node wrong, or the machine and the node both disagree with us
        return NODE_WRONG
    if any(k.startswith("divergence") for k in keys):
        return DIVERGENCE
    if "setup" in keys:
        return SETUP_MISMATCH
    return NO_PROGRESS if keys else 0


def status_line(st: State, alarms: dict[str, str]) -> str:
    return (
        f"{_now()} block {st.finalized}: {len(st.inputs)} inputs, {len(st.replayed)} replayed"
        f"{' (stopped: ' + st.blocked.split(':')[0] + ')' if st.blocked else ''}, "
        f"{len(alarms)} alarm(s) standing"
    )


def run_watch(cfg: Config, interval: float, once: bool = False) -> int:
    cfg.dir.mkdir(parents=True, exist_ok=True)
    lock = open(cfg.dir / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another micronode watch is using {cfg.dir}")
    path = cfg.dir / STATE_FILE
    st = State.load(path)
    if st.app_address:
        print(f"resuming: {len(st.replayed)} inputs replayed, {len(st.raised)} alarm(s) already sent")
    try:
        while True:
            alarms = tick(cfg, st)
            st.save(path)
            print(status_line(st, alarms), flush=True)
            if once:
                return exit_code(alarms)
            time.sleep(interval)
    except KeyboardInterrupt:
        st.save(path)
        return 0
    finally:
        lock.close()
