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

1. **fetch:** reads `InputAdded` from the InputBox up to the `finalized` block by default (`--to-block` accepts a number or a tag), writing each input's raw bytes, which are already EvmAdvance-encoded. Reading below finality would make the micronode itself raise false alarms on reorgs.
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
- **Which hash is compared:** the CLI prints two hashes before input N: the boundary state, and the state with the input loaded. The state after input N is the boundary hash of input N+1, or `--final-hash` for the last input. The node records `GetRootHash` right after each advance (rollups-node `pkg/machine`), which is the same state. For a rejected input, both sides record the reverted state.
- **Emulator versions:** both 0.21 and 0.22 work. The two releases name the input range differently (`input_index_*` in 0.21, `input_file_index_*` in 0.22), and `run` detects which one the CLI accepts. Any image with `cartesi-machine` on its PATH can be used, including the rollups-node image: `--image ghcr.io/cartesi/rollups-node:<tag>`.
- **Replay range:** every run replays from input 0. Resuming from a stored snapshot is not implemented yet.
- **Reverts:** the machine runs with `--remote-spawn` because the CLI's default revert mode forks a remote machine to undo rejected inputs.

## Checked against a live rollups-node (2026-11-05)

The setup was a devnet stack with rollups-node `v2.0.0-alpha.13` (Go node, PRT mode) and the tester app (emulator 0.21). Eight inputs went straight to the InputBox:

- notices;
- invalid JSON (rejected);
- reports;
- mixed outputs;
- an ERC-20 withdrawal voucher;
- a portal deposit (rejected by the app);
- large notices;
- `force_exception`.

`micronode check` read the inputs from the chain, not from the node. It matched the node's `machine_hash` on all eight inputs, including the rejected ones and the final `EXCEPTION`.

Two negative controls each fired at the right input and exited with 2:

- **One payload byte changed on the micronode side:** the first divergence was at that input, and every later input diverged too.
- **The node's recorded `machine_hash` for one input forced to a wrong value in its Postgres:** the divergence was at that input only. The node protects completed results with a trigger (`completed input result is immutable`), so the trigger had to be disabled for the test.

### Reorg below finality (F-10, A13-03)

This scenario reproduces a known finding. The node was configured to read the chain at `latest`, which is not the default and is documented as unsafe. An `evm_revert` replaced input 0 (payload `0x41414141`, "A") with another input at the same index (`0x42424242`, "B"). The node kept the orphaned input A and claimed with it. The micronode reads the InputBox only up to `finalized`, so it replayed the canonical input B.

| Time | Event |
|---|---|
| 15:55:38 | Input B is finalized. The micronode reports a divergence at input 0. |
| 16:05:20 | The node marks the app `DIVERGED` ("inconsistent commitment between off-chain and on-chain"). |

The micronode raised the alarm about 9 minutes 40 seconds earlier, as soon as the input was final. Before that it raised nothing, because the inputs it reads are only the final ones.

In the control run (`NODE_DEFAULT_BLOCK=finalized`, the same reorg), the node kept input B and epoch 1 was accepted. The micronode was polled every 20 s for about 13 minutes and agreed with the node in every round, so the reorg raised no false alarm.

### Node restarts

The setup was the tester app on rollups-node alpha.13, with snapshot policy `NONE` (the default). The micronode agreed with the node after every restart, which shows the node came back with the right state. The node's logs do not show that, because they do not record the replay.

| Case | What the node did | Micronode |
|---|---|---|
| `docker restart` of the advancer between inputs | reloaded the template, replayed, and processed the inputs that were waiting | 7/7 agree |
| `SIGKILL` while a 4.8 MB input was being processed | left the input with status `NONE` (no partial result), reloaded the template, and processed it again | 14/14 agree |

With `EVERY_INPUT`, the advancer could not write the snapshot, because the test stack points `CARTESI_SNAPSHOTS_DIR` at the read-only template mount. It destroyed the runtime, set the app to `FAILED`, and processed nothing more until an operator ran `cartesi-rollups-cli app status app enabled`. That command asks the operator to "verify snapshot/database consistency" first. `micronode compare` does that check: it confirmed the processed inputs before re-enabling, and after recovery all inputs agreed. While the app was stopped, though, the micronode only reported "not yet processed". It has no alarm yet for a node that stops making progress.

## Tests

```bash
python3 -m unittest -v
```
