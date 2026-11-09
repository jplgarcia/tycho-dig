"""Driving the ``cartesi-machine`` CLI and parsing what it prints per input."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE = "cartesi/machine-emulator:0.22.0"

_BEFORE_RE = re.compile(r"^Before input (\d+)$")
_HASH_RE = re.compile(r"^(\d+): (0x[0-9a-f]{64})$")
_YIELD_RE = re.compile(r"^Manual yield (rx-accepted|rx-rejected|tx-exception)\b")


@dataclass
class InputResult:
    index: int
    hash_before: str  # machine root hash at the input boundary, before the input is loaded
    hash_loaded: str  # same, right after the input is written into the rx buffer
    hash_after: str | None = None  # after processing: what the node records for this input
    outcome: str | None = None  # rx-accepted, rx-rejected, tx-exception
    outputs_merkle_root: str | None = None


@dataclass
class RunResult:
    inputs: list[InputResult] = field(default_factory=list)
    final_hash: str | None = None
    log: str = ""
    emulator_version: str | None = None

    @property
    def initial_hash(self) -> str | None:
        """State hash of the loaded template, at the boundary before input 0."""
        return self.inputs[0].hash_before if self.inputs and self.inputs[0].index == 0 else None


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
    return result


def _cli_args(begin: int, end: int, template: str, final_hash: bool, legacy: bool = False) -> list[str]:
    # emulator 0.21 names the range input_index_begin/end; 0.22 input_file_index_begin/end
    key = "input_index" if legacy else "input_file_index"
    advance = ",".join(
        [
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
    )
    args = [
        "--remote-spawn",
        "--remote-address=127.0.0.1:0",
        f"--load={template}",
        f"--cmio-advance-state={advance}",
    ]
    if final_hash:
        args.append("--final-hash")
    return args


def _probe(prefix: list[str]) -> tuple[bool, str | None]:
    """(legacy, version) of a cartesi-machine CLI.

    legacy is True when the CLI predates input_file_index_* (emulator <= 0.21).
    """
    proc = subprocess.run([*prefix, "--help"], capture_output=True, text=True)
    help_text = proc.stdout + proc.stderr
    legacy = "input_file_index_begin" not in help_text and "input_index_begin" in help_text
    proc = subprocess.run([*prefix, "--version"], capture_output=True, text=True)
    first = (proc.stdout + proc.stderr).strip().splitlines()
    version = first[0].removeprefix("cartesi-machine").strip() if first else None
    return legacy, version


def run(
    template: Path,
    workdir: Path,
    begin: int,
    end: int,
    image: str = DEFAULT_IMAGE,
    native: bool = False,
) -> RunResult:
    """Replay input files ``workdir/input-<begin..end-1>.bin`` from the template.

    ``native`` uses a ``cartesi-machine`` on PATH; otherwise the CLI runs in a
    throwaway Docker container with the template mounted read-only.
    """
    template = template.resolve()
    workdir = workdir.resolve()
    if native:
        exe = shutil.which("cartesi-machine")
        if not exe:
            raise RuntimeError("cartesi-machine is not on PATH (drop --native to use Docker)")
        legacy, version = _probe([exe])
        cmd = [exe, *_cli_args(begin, end, str(template), True, legacy)]
        cwd = str(workdir)
    else:
        legacy, version = _probe(["docker", "run", "--rm", "--entrypoint", "cartesi-machine", image])
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{template}:/template:ro",
            "-v", f"{workdir}:/work",
            "-w", "/work",
            "--user", f"{os.getuid()}:{os.getgid()}",
            # any image with cartesi-machine on PATH works (e.g. the rollups-node image)
            "--entrypoint", "cartesi-machine",
            image,
            *_cli_args(begin, end, "/template", True, legacy),
        ]
        cwd = None
    # the application may print arbitrary bytes to the console
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    text = proc.stdout + proc.stderr
    result = parse_run_log(text)
    result.emulator_version = version
    # An input that raises an exception is terminal: the CLI stops there and exits 1.
    # That is an outcome to compare, not a failure of the run.
    ended_in_exception = bool(result.inputs) and result.inputs[-1].outcome == "tx-exception"
    if proc.returncode != 0 and not ended_in_exception:
        raise RuntimeError(f"cartesi-machine failed (exit {proc.returncode}):\n{text[-3000:]}")
    for item in result.inputs:
        root = workdir / f"root-{item.index}.bin"
        if root.exists():
            item.outputs_merkle_root = "0x" + root.read_bytes().hex()
    return result
