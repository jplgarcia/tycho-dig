# micronode

A Cartesi Machine with no node around it. You give it inputs and it prints the machine hash after each one. You can then compare those hashes with what a rollups-node recorded.

The point is implementation diversity. The micronode does not take part in disputes. It is an independent check that can tell exactly which input made the node diverge. It is written in Python with the standard library only, and it drives the `cartesi-machine` CLI, by default through Docker.

## What it catches, and what it does not

All implementations run the same emulator. So when the micronode gets the same inputs in the same order, it computes the same hashes as the node's machine. A divergence therefore points at the node's handling of the machine, not at the machine itself:

- an input that is wrong, skipped, duplicated or out of order (the micronode reads the InputBox itself);
- state lost or corrupted across snapshots or restarts;
- the hash or outputs root being recorded wrongly.

It cannot see bugs in the emulator or in the application, because both sides execute them identically.

## Usage

```bash
pip install -e .            # or: python3 -m micronode ...
```

### Simulate by hand

```bash
micronode craft --dir work --payload-text "hello"            # writes work/input-0.bin
micronode craft --dir work --sender 0xabc... --payload 0x1234 # input-1.bin
micronode show work/input-1.bin
micronode run --dir work --template path/to/app-template
```

`run` replays `input-0.bin, input-1.bin, ...` from the template. For every input it prints the outcome (rx-accepted, rx-rejected or tx-exception), the machine hash after the input, and the outputs Merkle root. It also writes `run.json`, `run.log`, and the outputs and reports.

### Check a deployed application

```bash
micronode check --dir work \
  --template path/to/app-template \
  --rpc https://<an Ethereum RPC, ideally not the one the node uses> \
  --node http://localhost:10011/rpc \
  --app <app name or address>
```

`check` runs three steps:

1. **fetch:** reads `InputAdded` from the InputBox, writing each input's raw bytes, which are already EvmAdvance-encoded.
2. **run:** replays the inputs from the template.
3. **compare:** compares the per-input hashes with the `machine_hash` that `cartesi_listInputs` returns.

It exits with 2 at the first divergent input. You can also run `fetch`, `run` and `compare` separately.

## Test template

`scripts/make-echo-template.sh` builds a template from a kernel and `rootfs-tools.ext2` that match the emulator version. The template runs `ioctl-echo-loop`, which echoes every input as a voucher, a notice and a report.

```bash
scripts/make-echo-template.sh linux.bin rootfs-tools.ext2 work/echo-template
```

## Notes

- **Matching versions:** the template must have been stored by the same emulator version as `--image` (default `cartesi/machine-emulator:0.22.0`). A 0.21 template fails to load on 0.22 with `expected "archive_version" 8 (got 7)`.
- **Which hash is compared:** the CLI prints two hashes before input N: the boundary state, and the state with the input loaded. The state after input N is the boundary hash of input N+1, or `--final-hash` for the last input. The node records `GetRootHash` right after each advance (rollups-node `pkg/machine`), which should be the same state. This is inferred from the code and not yet checked against a running node.
- **Replay range:** every run replays from input 0. Resuming from a stored snapshot is not implemented yet.
- **Reverts:** the machine runs with `--remote-spawn` because the CLI's default revert mode forks a remote machine to undo rejected inputs.

## Tests

```bash
python3 -m unittest -v
```
