# micronode

A Cartesi Machine with no node around it. You give it inputs and it prints the machine hash after each one. You can then compare those hashes with what a rollups-node recorded.

The point is implementation diversity. The micronode does not take part in disputes. It is an independent check that can tell exactly which input made the node diverge. It is written in Python with the standard library only, and it drives the `cartesi-machine` CLI, by default through Docker.

## What it catches, and what it does not

All implementations run the same emulator. So when the micronode gets the same inputs in the same order, it computes the same hashes as the node's machine. A divergence therefore points at the node's handling of the machine, not at the machine itself:

- an input that is wrong, skipped, duplicated or out of order (the micronode reads the InputBox itself);
- state lost or corrupted across snapshots or restarts;
- the hash or outputs root being recorded wrongly.

It cannot see bugs in the emulator or in the application, because both sides execute them identically. One application bug it does catch: a guest that declares an outputs Merkle root that does not match the outputs it emitted (see [Outputs root](#outputs-root)).

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

`run` replays `input-0.bin, input-1.bin, ...` from the template. For every input it prints the outcome (rx-accepted, rx-rejected or tx-exception), the machine hash after the input, and the outputs Merkle root the machine declared. It also writes `run.json`, `run.log`, and the outputs and reports. It exits with 5 when a declared outputs root is wrong (see [Outputs root](#outputs-root)).

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

You can also run `fetch`, `run` and `compare` separately.

### Exit codes and alarms

`check` and `compare` exit with one of these codes:

| Exit | Meaning |
|---|---|
| 0 | All is well. |
| 2 | **Divergence:** the first input where the node's hash differs from the micronode's. |
| 3 | **Progress alarm:** the node stopped working on the app. |
| 4 | **Setup mismatch:** the micronode is not running the application's machine, so nothing was compared. |
| 5 | **Machine wrong:** the outputs root the machine declared differs from the root of the outputs it emitted. |
| 6 | **Node wrong:** the node's outputs root differs from the micronode's, while the machine's root agrees with it. |

When several fire, the exit code is the first of 5, 6, 2, 3. Every alarm is printed either way.

Before comparing anything, `check` verifies that the state hash of the template the micronode loaded (the hash at the boundary before input 0) equals the application contract's `getTemplateHash()`. `compare` checks it against the node's `template_hash` instead.

A different emulator version gives a different hash: for example, 0.23 changes the default MIMPID, so every state hash changes. A different template does too. When the hashes differ, every per-input hash would differ with no bug anywhere, so the micronode refuses instead of reporting a divergence. `run` prints the emulator version and the template state hash.

A progress alarm (`PROGRESS ALARM: ...`) fires in two cases:

- **The app is stopped in the node:** its `status` is anything but `OK` (`FAILED`, `DIVERGED`, `CORRUPTED`, `GUEST_EXCEPTION`, ...), or it is disabled. `FAILED` needs an operator; the others are terminal.
- **The node is behind:** a finalized input is still unprocessed more than `--max-lag-blocks` blocks after the finalized head (default 25; a negative value disables the check). This catches a node that died or hangs while its status still says `OK`.

Both checks are stateless, so `check` can run from cron or a loop without keeping anything between runs.

### Outputs root

When the guest accepts an input, it writes the root of its outputs Merkle tree into the TX buffer. The tree holds every accepted output since genesis, and it is what the epoch's claim settles on-chain. A guest that writes the wrong root makes every output of that epoch unprovable (red-team item RT-1).

`run` recomputes the root itself after every accepted input, from the outputs the machine emitted. Its tree follows libcmt: height 63, leaf `keccak256(output)`. It then compares that root with the one the machine declared. A mismatch prints `OUTPUTS ROOT MISMATCH (machine wrong)` with the input, the declared root, and the recomputed root.

The CLI has the same check built in (`check_outputs_merkle_root`, on by default). When it fires, the CLI stops with `outputs Merkle root mismatch at input N` and exits with 1. The micronode turns that check off and keeps replaying, so the machine hashes of later inputs can still be compared with the node's. With a CLI that cannot turn it off, the micronode reads the CLI's assertion as the same detection.

The Go node checks the root only when the epoch closes, in its validator. It then marks the app `INVALID_OUTPUTS_ROOT`, with both roots in the reason. `compare` reads them and runs Diego's cross-check:

| Node declared | Node calculated | Micronode: machine | Micronode: recomputed | Verdict |
|---|---|---|---|---|
| bad | good | bad | good | **machine wrong** (exit 5): escalate to the guardian |
| good | bad | good | good | **node wrong** (exit 6): fix the node, do not foreclose |

The micronode's "machine wrong" alarm fires on its own, at the bad input, even before the node closes the epoch. Only the root after an epoch's last accepted input reaches the chain. The micronode reports every input, as the CLI does.

The micronode shares the emulator and the template with the node, so an emulator bug would not show here. The tree code is its own, Python's `hashlib` KECCAK-256 (OpenSSL 3.2 or later) or a pure-Python fallback. It shares nothing with libcmt or the node.

## Test template

`scripts/make-echo-template.sh` builds a template from a kernel and `rootfs-tools.ext2` that match the emulator version. The template runs `ioctl-echo-loop`, which echoes every input as a voucher, a notice and a report.

```bash
scripts/make-echo-template.sh linux.bin rootfs-tools.ext2 work/echo-template
```

## Notes

- **Matching versions:** the template must have been stored by the same emulator version as `--image` (default `cartesi/machine-emulator:0.22.0`). A 0.21 template fails to load on 0.22 with `expected "archive_version" 8 (got 7)`. Even when a template loads, the micronode must run the version the application was deployed with, or the template guard refuses (exit 4).
- **Emulator 0.23 (pre-release `v0.23.0-test2`), not tested yet:**
  - The default MIMPID changes from 22 to 23, so state hashes differ from 0.22.
  - Stored machines use archive version 9, so 0.22 snapshots do not load.
  - The JSON-RPC protocol is 0.9.0.
  - A 0.23 micronode can only check an application deployed with a 0.23 template. Against a 0.22 app it exits with 4.
  - The CLI's on-chain proof output is changing: it will also prove that the machine is in a manual yield with reason accept. The micronode does not use the CLI's proof files, so this does not affect it.
- **Which hash is compared:** the CLI prints two hashes before input N: the boundary state, and the state with the input loaded. The state after input N is the boundary hash of input N+1, or `--final-hash` for the last input. The node records `GetRootHash` right after each advance (rollups-node `pkg/machine`), which is the same state. For a rejected input, both sides record the reverted state.
- **Emulator versions:** both 0.21 and 0.22 work. The two releases name the input range differently (`input_index_*` in 0.21, `input_file_index_*` in 0.22), and `run` detects which one the CLI accepts. Any image with `cartesi-machine` on its PATH can be used, including the rollups-node image: `--image ghcr.io/cartesi/rollups-node:<tag>`.
- **Replay range:** every run replays from input 0. Resuming from a stored snapshot is not implemented yet.
- **Reverts:** the machine runs with `--remote-spawn` because the CLI's default revert mode forks a remote machine to undo rejected inputs. It also passes `--remote-shutdown`, so the CLI stops that server on exit, even after an error. Without the flag the CLI prints `Left alive JSONRPC remote cartesi machine`. As a second guard, a native run kills the CLI's process group when it ends, and a Docker run removes its container by name.

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

With `EVERY_INPUT`, the advancer could not write the snapshot, because the test stack points `CARTESI_SNAPSHOTS_DIR` at the read-only template mount. It destroyed the runtime, set the app to `FAILED`, and processed nothing more until an operator ran `cartesi-rollups-cli app status app enabled`. That command asks the operator to "verify snapshot/database consistency" first. `micronode compare` does that check: it confirmed the processed inputs before re-enabling, and after recovery all inputs agreed. The progress alarm (exit 3) now catches both stalls:

- a `FAILED` app, at once, from its status;
- a stopped advancer whose status still said `OK`, after the lag limit. One input was final for 33 blocks against a limit of 25.

After recovery, `check` exits with 0 again.

### Outputs root mismatch (RT-1, 2026-11-09)

A test guest emitted its notices, then accepted input 1 with `0x5a...5a` in the TX buffer instead of its outputs root. The run used rollups-node `v2.0.0-alpha.13` and the `cartesi-machine` 0.21.0 from the node image. The node marked the app `INVALID_OUTPUTS_ROOT` when epoch 1 closed.

| Step | Result |
|---|---|
| `micronode run` on the run's two inputs | the template matches the app's (`0xbc2ff7bd...c85f`); input 1 is flagged `OUTPUTS ROOT MISMATCH (machine wrong)`: declared `0x5a...5a`, recomputed `0xb2b7...c2e6`; exit 5 |
| `micronode compare` against the node's recorded JSON-RPC answers | both machine hashes agree with the node; the node's calculated root equals ours, so the verdict is **machine wrong**; exit 5 |
| Control: the same guest without the bad root | no mismatch; exit 0 |

Before this, the CLI's own check stopped the replay with exit 1, the micronode crashed with a Python `RuntimeError`, and none of its alarms fired. The unit tests replay that run's files, and they also cover the "node wrong" case: a simulated node that drops the last output.

## Tests

```bash
python3 -m unittest -v
```
