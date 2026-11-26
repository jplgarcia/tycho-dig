"""Replay up to input K from one of crisium's checkpoints instead of from input 0.

crisium (machine-emulator 0.23, doc/recipes/prt-stored-player.lua) stores, as soon as it has
computed epoch N-1, the machine epoch N starts from in ``checkpoints/epoch-N/``: a stored machine,
plus ``epoch.json`` (version 2) and ``outputs-frontier.json`` (the outputs tree since genesis).
In ``epoch.json``, ``final_state_hash`` is the state epoch N starts from (load_epoch_checkpoint
opens epoch N from it); ``initial_state_hash`` is where epoch N-1 started.

At any moment epoch N-1 is accepted, N is sealed (its tournament runs) and N+1 is open. DaveConsensus
seals N+1 only when N is accepted, so:
  - ``epoch-N`` holds N-1's accepted end: EpochSealed(N).initialMachineStateHash anchors it on chain;
  - ``epoch-(N+1)`` holds N's end as crisium computed it: until N is accepted, only crisium
    vouches for it (EpochStaged(N) and the node can cross-check it, not anchor it).
By default the replay starts from the newest anchored checkpoint at or below K: the epoch holding
K if it is sealed, else the newest sealed epoch, replayed through into the open one.
``unanchored_latest`` starts from the open epoch's checkpoint instead and says so.

Nothing from crisium is trusted beyond that. crisium's directory must be read-only for the
tycho-dig (a ``:ro`` mount; a Docker volume is mounted read-only by tycho-dig itself), it is
copied into the work directory first (crisium deletes old checkpoints), and only the copy is loaded,
with its hash tree rebuilt.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from . import chain, machine
from .outputs import OutputsTree, load_frontier

RUN_FILE = "run.json"
CHECKPOINT_COPY = "checkpoint"

OK, DIVERGENCE, SETUP_MISMATCH, MACHINE_WRONG = 0, 2, 4, 5
ZERO = "0x" + "00" * 32


@dataclass
class Config:
    dir: Path
    checkpoints: str  # crisium's checkpoints directory: a host path, or a path inside `volume`
    rpc: str
    app: str
    image: str | None
    native: bool
    epoch: int | None = None  # the checkpoint to start from (default: chosen from `to`)
    to: int | None = None  # last input to replay, a global index (default: the start epoch's last)
    volume: str | None = None  # a Docker volume holding `checkpoints`, mounted read-only
    unanchored_latest: bool = False
    node: str | None = None
    node_app: str | None = None  # the application as the node knows it (default: its address)
    from_block: int | None = None
    to_block: str = "finalized"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.timings: dict[str, float] = {}
        self._t = time.monotonic()

    def check(self, what: str, ours: str | None, theirs: str | None, kind: str) -> bool:
        ok = ours is not None and theirs is not None and ours.lower() == theirs.lower()
        self.checks.append({"check": what, "ok": ok, "tycho-dig": ours, "reference": theirs, "kind": kind})
        mark = "ok" if ok else kind.upper()
        print(f"  [{mark}] {what}\n         tycho-dig {ours}\n         reference {theirs}")
        return ok

    def lap(self, name: str) -> None:
        now = time.monotonic()
        self.timings[name] = round(now - self._t, 2)
        self._t = now


# --- copying the checkpoint, read-only --------------------------------------------------------


def assert_read_only(source: Path) -> None:
    """crisium's checkpoint must not be writable by us: the OS enforces the rule, not our code."""
    writable = [p for p in [source, *source.iterdir()] if os.access(p, os.W_OK)]
    if writable:
        raise RuntimeError(
            f"{source} is writable by tycho-dig ({writable[0].name}): mount crisium's data read-only "
            "(e.g. -v <volume>:/crisium:ro) or pass --volume, which mounts it read-only"
        )


def copy_checkpoint(source: Path, dest: Path) -> None:
    """Copy a stored machine directory without making its sparse files dense where possible.

    APFS clones on macOS, reflinks or sparse copies on Linux; a plain copy otherwise.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (source / "epoch.json").exists() or not (source / "config.json").exists():
        raise RuntimeError(f"{source} is not a crisium checkpoint (no epoch.json or config.json)")
    assert_read_only(source)
    if platform.system() == "Darwin":
        attempts = [["cp", "-cR", str(source), str(dest)], ["cp", "-R", str(source), str(dest)]]
    else:
        attempts = [["cp", "-R", "--reflink=auto", "--sparse=always", str(source), str(dest)]]
    for cmd in attempts:
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            _make_writable(dest)
            return
        shutil.rmtree(dest, ignore_errors=True)
    try:
        shutil.copytree(source, dest)
    except FileNotFoundError as e:
        raise RuntimeError(f"{source} changed while being copied (crisium removed it?): {e}") from e
    _make_writable(dest)


def _make_writable(dest: Path) -> None:
    # the copy is ours: a read-only source keeps its modes, and the next run must be able to remove it
    for p in [dest, *dest.iterdir()]:
        p.chmod(p.stat().st_mode | 0o200)


def copy_from_volume(volume: str, path: str, dest: Path, image: str) -> None:
    """Copy <volume>/<path> into dest through a container that mounts the volume read-only."""
    inner = PurePosixPath("/crisium") / PurePosixPath(path).relative_to("/")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # root in the container: crisium writes epoch.json and the frontier readable by its own user
    # only. The :ro mount still makes the volume read-only for root; the copy is then handed to us.
    cmd = [
        "docker", "run", "--rm", "-v", f"{volume}:/crisium:ro", "-v", f"{dest.parent}:/work",
        "--user", "0:0", "--entrypoint", "sh", image, "-c",
        'test -f "$1/epoch.json" && test -f "$1/config.json" || { echo "$1 is not a crisium checkpoint" >&2; exit 3; }; '
        '{ cp -R --sparse=always "$1" "/work/$2" 2>/dev/null || cp -R "$1" "/work/$2"; } && chown -R "$3" "/work/$2"',
        "copy", str(inner), dest.name, f"{os.getuid()}:{os.getgid()}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"cannot copy {inner} from volume {volume} (gone, or crisium removed it?): "
                           f"{proc.stderr.strip()}")
    _make_writable(dest)


# --- helpers --------------------------------------------------------------------------------


def _hex(raw: str | None) -> str | None:
    return raw.lower() if isinstance(raw, str) else None


def _print_table(result: machine.RunResult) -> None:
    print(f"{'input':>6}  {'outcome':<12} {'machine hash after input':<66}  outputs root declared / recomputed")
    for item in result.inputs:
        flag = "  <- MISMATCH" if item.outputs_root_mismatch else ""
        print(f"{item.index:>6}  {item.outcome or '?':<12} {item.hash_after or '?':<66}  "
              f"{item.outputs_merkle_root or '-'}{flag}")
        if item.outputs_merkle_root:
            print(f"{'':>87}{item.independent_outputs_root}")


def choose_start(cfg: Config, sealed: dict) -> tuple[int, int, bool]:
    """(start epoch, its first input, anchored on chain) for the requested replay."""
    latest = max(sealed) if sealed else None
    if latest is None:
        raise SystemExit("no epoch is sealed yet")
    open_epoch, open_lower = latest + 1, sealed[latest].input_upper
    if cfg.epoch is not None:
        if cfg.epoch in sealed:
            if cfg.unanchored_latest:
                raise SystemExit(f"--unanchored-latest starts from the open epoch {open_epoch}, not {cfg.epoch}")
            return cfg.epoch, sealed[cfg.epoch].input_lower, True
        if cfg.epoch == open_epoch:
            if not cfg.unanchored_latest:
                raise SystemExit(
                    f"epoch {open_epoch} is open: its checkpoint is anchored only by crisium. Start from epoch "
                    f"{latest} (the default for an input of epoch {open_epoch}), or pass --unanchored-latest")
            return open_epoch, open_lower, False
        raise SystemExit(f"epoch {cfg.epoch} is neither sealed nor the open epoch {open_epoch}")
    if cfg.to is None:
        raise SystemExit("give --to K, --epoch N, or both")
    if cfg.unanchored_latest:
        if cfg.to < open_lower:
            raise SystemExit(f"--unanchored-latest is for inputs of the open epoch {open_epoch} (from {open_lower})")
        return open_epoch, open_lower, False
    for epoch, seal in sorted(sealed.items()):
        if seal.input_lower <= cfg.to < seal.input_upper:
            return epoch, seal.input_lower, True
    if cfg.to >= open_lower:
        return latest, sealed[latest].input_lower, True  # through the sealed epoch into the open one
    raise SystemExit(f"input {cfg.to} is in no sealed epoch")


# --- the mode -------------------------------------------------------------------------------


def run_from_checkpoint(cfg: Config) -> int:
    rep = Report()
    workdir = cfg.dir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # --- what the chain says --------------------------------------------------------------------
    consensus = chain.call_address(cfg.rpc, cfg.app, "getOutputsMerkleRootValidator()")
    inputbox = chain.call_address(cfg.rpc, consensus, "getInputBox()")
    first_block = cfg.from_block
    if first_block is None:
        first_block = int.from_bytes(chain.call_word(cfg.rpc, consensus, "getDeploymentBlockNumber()"), "big")
    to_block = chain.resolve_block(cfg.rpc, cfg.to_block)
    sealed, staged = chain.epoch_events(cfg.rpc, consensus, None, first_block, to_block)
    rep.lap("chain_events_s")
    n, lower, anchored = choose_start(cfg, sealed)
    latest = max(sealed)
    if n in sealed:
        upper = sealed[n].input_upper
        last = upper - 1 if cfg.to is None else cfg.to
    else:
        upper, last = None, cfg.to  # the open epoch: its end is not fixed yet
        if last is None:
            raise SystemExit(f"epoch {n} is open: give --to K")
    if last < lower - 1 or (last == lower - 1 and upper != lower):
        raise SystemExit(f"--to {cfg.to} is below epoch {n}'s first input {lower}")
    print(f"application {cfg.app}, consensus {consensus}, InputBox {inputbox}; up to block {to_block} "
          f"({cfg.to_block}): epochs 0..{latest} sealed, epoch {latest + 1} open")
    print(f"start: checkpoint of epoch {n} (first input {lower}), "
          + ("anchored on chain by EpochSealed" if anchored else "ANCHORED ONLY BY CRISIUM (--unanchored-latest)")
          + f"; replaying inputs {lower}..{last}")

    # --- the checkpoint: copy it (read-only source), then check the copy --------------------------
    copy = workdir / CHECKPOINT_COPY
    source = f"{cfg.checkpoints.rstrip('/')}/epoch-{n}"
    if cfg.volume:
        copy_from_volume(cfg.volume, source, copy, cfg.image or machine.DEFAULT_IMAGE)
        where = f"volume {cfg.volume} (mounted read-only): {source}"
    else:
        copy_checkpoint(Path(source), copy)
        where = source
    rep.lap("copy_checkpoint_s")
    manifest = json.loads((copy / "epoch.json").read_text())
    tree = load_frontier(copy / "outputs-frontier.json")
    start_root = "0x" + tree.root().hex()
    print(f"checkpoint {where}, copied to {copy}: {manifest.get('output_count')} outputs since genesis")
    print("checkpoint consistency:")
    setup_ok = manifest.get("version") == 2
    if not setup_ok:
        print(f"  [SETUP] epoch.json version {manifest.get('version')}, expected 2")
    setup_ok &= rep.check("outputs-frontier.json leaf count == epoch.json output_count",
                          str(tree.count), str(manifest.get("output_count")), "setup")
    setup_ok &= rep.check("outputs-frontier.json root == epoch.json outputs_merkle_root",
                          start_root, _hex(manifest.get("outputs_merkle_root")), "setup")
    start_hash = _hex(manifest.get("final_state_hash"))
    if anchored:
        seal = sealed[n]
        print(f"start anchor (on chain, EpochSealed({n}) at block {seal.block_number}):")
        setup_ok &= rep.check("epoch.json final_state_hash == EpochSealed initial state",
                              start_hash, seal.initial_machine_state_hash, "setup")
        # crisium seals epoch 0 with a zero outputs root (no history), later epochs with the settled root
        sealed_root = seal.outputs_merkle_root
        if n == 0 and sealed_root == ZERO:
            sealed_root = "0x" + OutputsTree().root().hex()
        setup_ok &= rep.check("outputs-frontier.json root == EpochSealed outputs root",
                              start_root, sealed_root, "setup")
        anchor_hash = seal.initial_machine_state_hash
    else:
        prev = n - 1
        print(f"start anchor: NONE on chain. Epoch {prev} is not accepted yet; this start is crisium's own "
              f"computation of epoch {prev}'s end. Cross-checks only:")
        anchor_hash = start_hash
        if prev in staged:
            setup_ok &= rep.check(f"epoch.json final_state_hash == EpochStaged({prev}) post-epoch state "
                                  "(staged, not accepted)", start_hash, staged[prev].post_epoch_machine_state_hash,
                                  "setup")
            setup_ok &= rep.check(f"outputs-frontier.json root == EpochStaged({prev}) outputs root",
                                  start_root, staged[prev].post_epoch_outputs_merkle_root, "setup")
        else:
            print(f"  epoch {prev} has no EpochStaged yet")
        if cfg.node and lower > 0:
            node_hash = chain.node_input_hashes(cfg.node, cfg.node_app or cfg.app).get(lower - 1)
            if node_hash is None:
                print(f"  the node has not processed input {lower - 1} yet")
            else:
                setup_ok &= rep.check(f"epoch.json final_state_hash == node's hash after input {lower - 1}",
                                      start_hash, node_hash, "setup")
    previous = sealed.get(n - 1)
    if previous is not None and manifest.get("input_count") is not None:
        rep.check(f"epoch.json input_count == epoch {n - 1}'s input count",
                  str(manifest["input_count"]), str(previous.input_upper - previous.input_lower), "warning")

    # --- the inputs, from the InputBox --------------------------------------------------------
    # inputs from `lower` on were added in the block of the seal before the start epoch or later;
    # inputs below a seal's upper bound by that seal's block
    scan_from = sealed[n - 1].block_number if n - 1 in sealed else first_block
    scan_to = sealed[n].block_number if n in sealed and upper is not None and last < upper else to_block
    manifest_inputs = []
    for item in chain.fetch_inputs(cfg.rpc, inputbox, cfg.app, scan_from, scan_to):
        if lower <= item.index <= last:
            (workdir / f"input-{item.index}.bin").write_bytes(item.raw)
            manifest_inputs.append({"index": item.index, "block_number": item.block_number,
                                    "transaction_hash": item.transaction_hash})
    got = sorted(i["index"] for i in manifest_inputs)
    if got != list(range(lower, last + 1)):
        raise RuntimeError(f"InputBox gave inputs {got} between blocks {scan_from} and {scan_to} "
                           f"({cfg.to_block}); expected {lower}..{last} (not final yet?)")
    (workdir / "inputs.json").write_text(json.dumps(
        {"app": cfg.app, "inputbox": inputbox, "start_epoch": n, "inputs": manifest_inputs}, indent=2))
    rep.lap("fetch_inputs_s")

    # --- the replay ---------------------------------------------------------------------------
    result = machine.run(copy, workdir, lower, last + 1, image=cfg.image or machine.DEFAULT_IMAGE,
                         native=cfg.native, epoch_start=True)
    rep.lap("replay_s")
    print(f"emulator {result.emulator_version or '?'}")
    label = "EpochSealed initial state" if anchored else "epoch.json final_state_hash (crisium)"
    setup_ok &= rep.check(f"loaded machine's state hash == {label}", result.loaded_hash, anchor_hash, "setup")
    if result.inputs:
        setup_ok &= rep.check(f"state before input {lower} == loaded state",
                              result.inputs[0].hash_before, result.loaded_hash, "setup")
    if not setup_ok:
        print(f"SETUP MISMATCH: the checkpoint is not the state epoch {n} starts from (wrong epoch, app, emulator\n"
              "  version, a damaged checkpoint, or crisium disagrees with the cross-checks). Not comparing anything.")
        _write(workdir, cfg, n, anchored, sealed, staged, result, rep, SETUP_MISMATCH)
        return SETUP_MISMATCH
    machine.verify_outputs_roots(result, workdir, tree)
    rep.lap("verify_roots_s")
    _print_table(result)
    replayed_to = result.inputs[-1].index if result.inputs else lower - 1
    if replayed_to < last:
        print(f"the machine stopped at input {replayed_to} ({result.inputs[-1].outcome}); "
              f"inputs {replayed_to + 1}..{last} were not run")

    code = OK
    mismatches = result.outputs_root_mismatches
    for item in mismatches:
        print(f"OUTPUTS ROOT MISMATCH (machine wrong): input {item.index}: the machine declared "
              f"{item.outputs_merkle_root}, its outputs give {item.independent_outputs_root}")
    if mismatches:
        code = MACHINE_WRONG

    # --- the Go node's per-input machine hashes -----------------------------------------------
    if cfg.node:
        theirs = chain.node_input_hashes(cfg.node, cfg.node_app or cfg.app)
        print("per-input machine hashes against the node (cartesi_listInputs):")
        for item in result.inputs:
            node_hash = theirs.get(item.index)
            if node_hash is None:
                print(f"  input {item.index}: the node has not processed it")
                continue
            if not rep.check(f"input {item.index} machine hash == node", item.hash_after, node_hash, "divergence"):
                code = code or DIVERGENCE
        rep.lap("node_compare_s")

    # --- the end of every sealed epoch the replay finished -------------------------------------
    hash_after = {i.index: i.hash_after for i in result.inputs}
    root_after, root = {}, start_root
    for item in result.inputs:
        if item.independent_outputs_root:
            root = item.independent_outputs_root
        root_after[item.index] = root
    epoch = n
    while epoch in sealed and sealed[epoch].input_upper - 1 <= replayed_to:
        end = sealed[epoch].input_upper - 1
        end_hash = hash_after.get(end, result.loaded_hash if end < lower else None)
        end_root = root_after.get(end, start_root if end < lower else None)
        print(f"epoch {epoch} end (after input {end}): state {end_hash}, outputs root {end_root}")
        compared = False
        if epoch in staged:
            compared = True
            for what, ours, theirs in (
                (f"epoch {epoch} end state == EpochStaged({epoch}) post-epoch state", end_hash,
                 staged[epoch].post_epoch_machine_state_hash),
                (f"epoch {epoch} end outputs root == EpochStaged({epoch}) outputs root", end_root,
                 staged[epoch].post_epoch_outputs_merkle_root),
            ):
                if not rep.check(what, ours, theirs, "divergence"):
                    code = code or DIVERGENCE
        if epoch + 1 in sealed:
            compared = True
            nxt = sealed[epoch + 1]
            for what, ours, theirs in (
                (f"epoch {epoch} end state == EpochSealed({epoch + 1}) initial state", end_hash,
                 nxt.initial_machine_state_hash),
                (f"epoch {epoch} end outputs root == EpochSealed({epoch + 1}) outputs root", end_root,
                 nxt.outputs_merkle_root),
            ):
                if not rep.check(what, ours, theirs, "divergence"):
                    code = code or DIVERGENCE
        if not compared:
            print(f"  epoch {epoch} has no EpochStaged and epoch {epoch + 1} is not sealed yet: its end is not compared")
        epoch += 1
    if code == DIVERGENCE:
        print("DIVERGENCE: the replay disagrees with the node or the chain (above)")
    if not anchored:
        print(f"NOTE: the start was anchored only by crisium (epoch {n - 1} not accepted); see the cross-checks above")
    _write(workdir, cfg, n, anchored, sealed, staged, result, rep, code)
    rep.lap("write_s")
    total = sum(rep.timings.values())
    print("timings (s): " + ", ".join(f"{k.removesuffix('_s')} {v}" for k, v in rep.timings.items())
          + f", total {round(total, 2)}")
    return code


def _write(workdir, cfg, n, anchored, sealed, staged, result, rep, code) -> None:
    near = range(n - 1, max([n, *sealed]) + 2)
    (workdir / RUN_FILE).write_text(json.dumps({
        "mode": "from-checkpoint",
        "start_epoch": n,
        "start_anchored_on_chain": anchored,
        "checkpoints": cfg.checkpoints,
        "volume": cfg.volume,
        "to": cfg.to,
        "exit_code": code,
        "emulator_version": result.emulator_version,
        "loaded_hash": result.loaded_hash,
        "final_hash": result.final_hash,
        "epoch_sealed": {e: asdict(sealed[e]) for e in near if e in sealed},
        "epoch_staged": {e: asdict(staged[e]) for e in near if e in staged},
        "checks": rep.checks,
        "timings_s": rep.timings,
        "outputs_root_mismatches": [i.index for i in result.outputs_root_mismatches],
        "inputs": [asdict(i) for i in result.inputs],
    }, indent=2))
    (workdir / "run.log").write_text(result.log)
