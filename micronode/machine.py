"""Driving the ``cartesi-machine`` CLI and parsing what it prints per input."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .outputs import OutputsTree

DEFAULT_IMAGE = "cartesi/machine-emulator:0.22.0"

_BEFORE_RE = re.compile(r"^Before input (\d+)$")
_HASH_RE = re.compile(r"^(\d+): (0x[0-9a-f]{64})$")
_YIELD_RE = re.compile(r"^Manual yield (rx-accepted|rx-rejected|tx-exception)\b")
_STORED_OUTPUT_RE = re.compile(r"^Storing (out-(\d+)-(\d+)\.bin)$")
# the CLI's own host-side root check (check_outputs_merkle_root:true, its default)
_CLI_ROOT_MISMATCH_RE = re.compile(r"outputs Merkle root mismatch at input (\d+)")

# files a run writes into the work directory; removed before each run so that a
# previous run's outputs or roots are never read as this run's
_RUN_FILE_GLOBS = ("out-*-*.bin", "root-*.bin", "rep-*-*.bin", "rejected-output-*-input-*.bin")


@dataclass
class InputResult:
    index: int
    hash_before: str  # machine root hash at the input boundary, before the input is loaded
    hash_loaded: str  # same, right after the input is written into the rx buffer
    hash_after: str | None = None  # after processing: what the node records for this input
    outcome: str | None = None  # rx-accepted, rx-rejected, tx-exception
    outputs_merkle_root: str | None = None  # declared by the guest in the TX buffer at accept
    independent_outputs_root: str | None = None  # recomputed here from the emitted outputs
    output_count: int | None = None  # accepted outputs since genesis, after this input

    @property
    def outputs_root_mismatch(self) -> bool:
        return (
            self.outputs_merkle_root is not None
            and self.independent_outputs_root is not None
            and self.outputs_merkle_root != self.independent_outputs_root
        )


@dataclass
class RunResult:
    inputs: list[InputResult] = field(default_factory=list)
    final_hash: str | None = None
    log: str = ""
    emulator_version: str | None = None
    # input at which the CLI's own root check stopped the run (only with a CLI that cannot turn it off)
    cli_root_check_stop: int | None = None

    @property
    def initial_hash(self) -> str | None:
        """State hash of the loaded template, at the boundary before input 0."""
        return self.inputs[0].hash_before if self.inputs and self.inputs[0].index == 0 else None

    @property
    def outputs_root_mismatches(self) -> list[InputResult]:
        return [item for item in self.inputs if item.outputs_root_mismatch]


def parse_run_log(text: str) -> RunResult:
    """Parse the stderr of ``cartesi-machine --cmio-advance-state=...,print_input_state_hashes --final-hash``.

    For every input the CLI prints "Before input N" and two hashes: the
    boundary state and the state with the input loaded. The state after
    input N is the boundary hash of input N+1, or the final hash for the
    last input.
    """
    result = RunResult(log=text)
    lines = [line.strip() for line in text.splitlines()]
    current: InputResult | None = None
    pending: list[str] = []
    extra: list[str] = []  # hashes printed after the last input's pair (--final-hash)
    for line in lines:
        m = _BEFORE_RE.match(line)
        if m:
            current = None
            pending = [m.group(1)]
            extra = []
            continue
        m = _HASH_RE.match(line)
        if m:
            if pending:
                pending.append(m.group(2))
                if len(pending) == 3:
                    if result.inputs:
                        result.inputs[-1].hash_after = pending[1]
                    current = InputResult(
                        index=int(pending[0]), hash_before=pending[1], hash_loaded=pending[2]
                    )
                    result.inputs.append(current)
                    pending = []
            else:
                extra.append(m.group(2))
            continue
        m = _YIELD_RE.match(line)
        if m and current is not None and current.outcome is None:
            current.outcome = m.group(1)
    if result.inputs and extra:
        result.final_hash = extra[-1]
        result.inputs[-1].hash_after = extra[-1]
    m = _CLI_ROOT_MISMATCH_RE.search(text)
    if m:
        result.cli_root_check_stop = int(m.group(1))
    return result


def stored_outputs(text: str) -> dict[int, list[tuple[int, str]]]:
    """Accepted output files of this run, per input: [(global output index, file name)].

    The CLI writes "out-%i-%o.bin" only for accepted inputs (a rejected input's
    outputs go to rejected-output-*), with %o the global output index.
    """
    out: dict[int, list[tuple[int, str]]] = {}
    for line in text.splitlines():
        m = _STORED_OUTPUT_RE.match(line.strip())
        if m:
            out.setdefault(int(m.group(2)), []).append((int(m.group(3)), m.group(1)))
    return out


def verify_outputs_roots(result: RunResult, workdir: Path, tree: OutputsTree | None = None) -> OutputsTree:
    """Recompute the outputs root after every accepted input and set independent_outputs_root.

    ``tree`` holds every accepted output before this run's first input (empty for a
    replay from genesis); it is extended in place and returned. Within a run the
    CLI numbers outputs consecutively; on a resumed run it numbers from 0 again,
    so only the order is used, not the CLI's global index.
    """
    files = stored_outputs(result.log)
    tree = tree if tree is not None else OutputsTree()
    previous = None
    for item in result.inputs:
        if item.outcome != "rx-accepted":
            continue  # rejected outputs never enter the tree; an exception ends the run
        for index, name in sorted(files.get(item.index, [])):
            if previous is not None and index != previous + 1:
                raise RuntimeError(f"output {name}: expected output index {previous + 1}")
            previous = index
            tree.append((workdir / name).read_bytes())
        item.independent_outputs_root = "0x" + tree.root().hex()
        item.output_count = tree.count
    return tree


@dataclass
class Caps:
    """What a cartesi-machine CLI supports."""

    legacy: bool = False  # names the range input_index_* (emulator <= 0.21)
    version: str | None = None
    root_check_option: bool = False  # check_outputs_merkle_root:<bool>
    remote_shutdown: bool = False  # --remote-shutdown


def _cli_args(
    begin: int,
    end: int,
    template: str,
    final_hash: bool,
    legacy: bool = False,
    root_check_option: bool = False,
    remote_shutdown: bool = False,
    store: str | None = None,
) -> list[str]:
    # emulator 0.21 names the range input_index_begin/end; 0.22 input_file_index_begin/end
    key = "input_index" if legacy else "input_file_index"
    advance = [
        "input:input-%i.bin",
        f"{key}_begin:{begin}",
        f"{key}_end:{end}",
        "print_input_state_hashes",
        "outputs_merkle_root:root-%i.bin",
        "output:out-%i-%o.bin",
        "report:rep-%i-%o.bin",
        "output_proof:",
        "outputs_merkle_root_proof:",
    ]
    if root_check_option:
        # the micronode checks the root itself (verify_outputs_roots): a mismatch is a detection to
        # report, and the replay goes on so the machine hashes of later inputs can still be compared
        advance.append("check_outputs_merkle_root:false")
    args = [
        "--remote-spawn",
        "--remote-address=127.0.0.1:0",
        f"--load={template}",
        f"--cmio-advance-state={','.join(advance)}",
    ]
    if final_hash:
        args.append("--final-hash")
    if remote_shutdown:
        # without it the CLI leaves the spawned JSON-RPC machine server running when it exits
        args.append("--remote-shutdown")
    if store:
        # stores the machine as it is after the last input, to resume from it next time
        args.append(f"--store={store}")
    return args


def _probe(prefix: list[str]) -> Caps:
    proc = subprocess.run([*prefix, "--help"], capture_output=True, text=True)
    help_text = proc.stdout + proc.stderr
    caps = Caps(
        legacy="input_file_index_begin" not in help_text and "input_index_begin" in help_text,
        root_check_option="check_outputs_merkle_root" in help_text,
        remote_shutdown="--remote-shutdown" in help_text,
    )
    proc = subprocess.run([*prefix, "--version"], capture_output=True, text=True)
    first = (proc.stdout + proc.stderr).strip().splitlines()
    caps.version = first[0].removeprefix("cartesi-machine").strip() if first else None
    return caps


def _execute(cmd: list[str], cwd: str | None, container: str | None) -> subprocess.CompletedProcess:
    """Run the CLI and make sure nothing it started outlives it, on every exit path.

    Natively the CLI runs in its own session, so its process group (the remote
    machine servers it spawned included) can be killed. In Docker, killing the
    `docker run` client does not stop the container, so it is removed by name.
    """
    # Output goes to files, not pipes: a server left alive inherits the CLI's stdout and
    # stderr, so reading pipes to EOF would wait for it forever. We wait for the CLI itself.
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_f, stderr=err_f, start_new_session=container is None)
        try:
            proc.wait()
            out_f.seek(0)
            err_f.seek(0)
            # the application may print arbitrary bytes to the console
            out = out_f.read().decode(errors="replace")
            err = err_f.read().decode(errors="replace")
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        finally:
            _reap(proc, container)


def _reap(proc: subprocess.Popen, container: str | None) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    finally:
        if container is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def run(
    template: Path,
    workdir: Path,
    begin: int,
    end: int,
    image: str = DEFAULT_IMAGE,
    native: bool = False,
    store: Path | None = None,
    need_root_check_option: bool = False,
) -> RunResult:
    """Advance input files ``workdir/input-<begin..end-1>.bin`` from a stored machine.

    ``template`` is the machine to load: the application's template for a replay
    from input 0, or a machine stored by an earlier run (``store``) to resume at
    ``begin``. ``store``, if given, must be inside ``workdir``. ``native`` uses a
    ``cartesi-machine`` on PATH; otherwise the CLI runs in a throwaway Docker
    container with ``workdir`` mounted and the template mounted read-only.
    """
    template = template.resolve()
    workdir = workdir.resolve()
    if store is not None:
        store = store.resolve()
        if workdir not in store.parents:
            raise ValueError(f"--store {store} must be inside {workdir}")
    for pattern in _RUN_FILE_GLOBS:
        for stale in workdir.glob(pattern):
            stale.unlink()
    if native:
        exe = shutil.which("cartesi-machine")
        if not exe:
            raise RuntimeError("cartesi-machine is not on PATH (drop --native to use Docker)")
        caps = _probe([exe])
        load, store_arg, mounts = str(template), str(store) if store else None, []
        prefix, cwd, container = [exe], str(workdir), None
    else:
        caps = _probe(["docker", "run", "--rm", "--entrypoint", "cartesi-machine", image])
        container = f"micronode-{uuid.uuid4().hex[:12]}"
        if workdir in template.parents:
            load, mounts = f"/work/{template.relative_to(workdir)}", []
        else:
            load, mounts = "/template", ["-v", f"{template}:/template:ro"]
        store_arg = f"/work/{store.relative_to(workdir)}" if store else None
        prefix = [
            "docker", "run", "--rm", "--name", container,
            *mounts,
            "-v", f"{workdir}:/work",
            "-w", "/work",
            "--user", f"{os.getuid()}:{os.getgid()}",
            # any image with cartesi-machine on PATH works (e.g. the rollups-node image)
            "--entrypoint", "cartesi-machine",
            image,
        ]
        cwd = None
    if need_root_check_option and not caps.root_check_option:
        raise RuntimeError(f"cartesi-machine {caps.version} cannot turn off check_outputs_merkle_root")
    cmd = [*prefix, *_cli_args(begin, end, load, True, caps.legacy, caps.root_check_option, caps.remote_shutdown, store_arg)]
    proc = _execute(cmd, cwd, container)
    text = proc.stdout + proc.stderr
    result = parse_run_log(text)
    result.emulator_version = caps.version
    if "Left alive JSONRPC remote cartesi machine" in text:
        print("warning: the CLI left a remote machine server alive (no --remote-shutdown)", file=sys.stderr)
    # An input that raises an exception is terminal: the CLI stops there and exits 1.
    # That is an outcome to compare, not a failure of the run. So is the CLI's own
    # root check stopping the run (a CLI that cannot turn it off): it is the detection.
    ended_in_exception = bool(result.inputs) and result.inputs[-1].outcome == "tx-exception"
    if proc.returncode != 0 and not ended_in_exception and result.cli_root_check_stop is None:
        raise RuntimeError(f"cartesi-machine failed (exit {proc.returncode}):\n{text[-3000:]}")
    for item in result.inputs:
        root = workdir / f"root-{item.index}.bin"
        if root.exists():
            item.outputs_merkle_root = "0x" + root.read_bytes().hex()
    return result
