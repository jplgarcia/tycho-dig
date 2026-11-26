"""A stand-in for `cartesi-machine` (and for `docker run` around it) in the unit tests.

It replays the RT-1 run (outgen guest, inputs "notices 1 8" and "notices 2 8 badroot"): it
copies that run's output and root files into the working directory and prints the CLI's
log lines for them. The machine hashes are placeholders; the files are the evidence's.

Environment:
  FAKE_OUTPUTS   directory with out-*.bin (the outputs the machine emitted)
  FAKE_ROOTS     directory with root-*.bin (the roots the machine declared)
  FAKE_CAPS      comma list of what the CLI supports: check_option, remote_shutdown
  FAKE_CRASH     if set, fail with an unrelated error after input 0
  FAKE_PIDFILE   where to write the pid of the "remote machine server" child
  FAKE_DOCKER_LOG  where `docker rm` calls are appended
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ACCEPT = "Manual yield rx-accepted (1) (0x000020 data)"
OUTPUTS = {0: ["out-0-0.bin"], 1: ["out-1-1.bin", "out-1-2.bin"]}


def h(n: int) -> str:
    return "0x" + f"{n:02x}" * 32


def cli(args: list[str], workdir: Path) -> int:
    caps = os.environ.get("FAKE_CAPS", "").split(",")
    if "--help" in args:
        help_text = "input_index_begin:<number>\n"
        if "check_option" in caps:
            help_text += "check_outputs_merkle_root:<boolean>\n"
        if "remote_shutdown" in caps:
            help_text += "  --remote-shutdown\n"
        print(help_text)
        return 0
    if "--version" in args:
        print("cartesi-machine 0.21.0")
        return 0
    advance = next(a for a in args if a.startswith("--cmio-advance-state="))
    check = "check_outputs_merkle_root:false" not in advance
    shutdown = "--remote-shutdown" in args
    server = subprocess.Popen(["sleep", "30"])  # the spawned JSON-RPC machine server
    Path(os.environ["FAKE_PIDFILE"]).write_text(str(server.pid))
    outputs, roots = Path(os.environ["FAKE_OUTPUTS"]), Path(os.environ["FAKE_ROOTS"])
    log = sys.stderr
    status = 0
    for index in (0, 1):
        print(f"Before input {index}\n0: {h(2 * index + 1)}\n0: {h(2 * index + 2)}", file=log)
        print(ACCEPT, file=log)
        for name in OUTPUTS[index]:
            shutil.copy(outputs / name, workdir / name)
            print(f"Storing {name}", file=log)
        shutil.copy(roots / f"root-{index}.bin", workdir / f"root-{index}.bin")
        print(f"Storing root-{index}.bin", file=log)
        if os.environ.get("FAKE_CRASH"):
            print("lua5.4: /usr/share/lua/5.4/cartesi-machine.lua:99: something else broke", file=log)
            status = 1
            break
        if check and (roots / f"root-{index}.bin").read_bytes() != _good_root(index):
            print(f"lua5.4: /usr/share/lua/5.4/cartesi-machine.lua:3038: outputs Merkle root mismatch at input {index}",
                  file=log)
            status = 1
            break
    else:
        print(f"0: {h(9)}", file=log)  # --final-hash
    if shutdown:
        server.kill()
        server.wait()
        print("Shutdown JSONRPC remote cartesi machine at '127.0.0.1:1'", file=log)
    else:
        print("Left alive JSONRPC remote cartesi machine at '127.0.0.1:1'", file=log)
    return status


def _good_root(index: int) -> bytes:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tycho_dig.outputs import outputs_root

    names = [n for i in range(index + 1) for n in OUTPUTS[i]]
    return outputs_root([(Path(os.environ["FAKE_OUTPUTS"]) / n).read_bytes() for n in names])


def docker(args: list[str]) -> int:
    if args[:2] == ["rm", "-f"]:
        with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
            f.write(f"rm -f {args[2]}\n")
        pidfile = Path(os.environ["FAKE_PIDFILE"])
        if pidfile.exists():  # removing the container kills everything in it
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass
        return 0
    assert args[0] == "run"
    workdir = Path.cwd()
    rest = args[1:]
    i = 0
    while i < len(rest):
        if rest[i] == "-v" and rest[i + 1].endswith(":/work"):
            workdir = Path(rest[i + 1].removesuffix(":/work"))
        if rest[i] in ("-v", "-w", "--user", "--entrypoint", "--name"):
            i += 2
            continue
        if rest[i] == "--rm":
            i += 1
            continue
        break
    return cli(rest[i + 1 :], workdir)  # skip the image name


if __name__ == "__main__":
    mode, argv = sys.argv[1], sys.argv[2:]
    sys.exit(docker(argv) if mode == "docker" else cli(argv, Path.cwd()))
