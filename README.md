# tycho-dig

(Formerly `micronode`. The name comes from the same source as crisium: in "2001", the anomaly is dug up in the Tycho crater.)

A Cartesi Machine with no node around it. You give it inputs and it prints the machine hash after each one. You can then compare those hashes with what a rollups-node recorded.

The point is implementation diversity. tycho-dig does not take part in disputes. It is an independent check that can tell exactly which input made the node diverge. It is written in Python with the standard library only, and it drives the `cartesi-machine` CLI, by default through Docker.

## What it catches, and what it does not

All implementations run the same emulator. So when tycho-dig gets the same inputs in the same order, it computes the same hashes as the node's machine. A divergence therefore points at the node's handling of the machine, not at the machine itself:

- an input that is wrong, skipped, duplicated or out of order (tycho-dig reads the InputBox itself);
- state lost or corrupted across snapshots or restarts;
- the hash or outputs root being recorded wrongly.

It cannot see bugs in the emulator or in the application, because both sides execute them identically. One application bug it does catch: a guest that declares an outputs Merkle root that does not match the outputs it emitted (see [Outputs root](#outputs-root)).

## Usage

```bash
pip install -e .            # or: python3 -m tycho_dig ...
```

### Simulate by hand

```bash
tycho-dig craft --dir work --payload-text "hello"            # writes work/input-0.bin
tycho-dig craft --dir work --sender 0xabc... --payload 0x1234 # input-1.bin
tycho-dig show work/input-1.bin
tycho-dig run --dir work --template path/to/app-template
```

`run` replays `input-0.bin, input-1.bin, ...` from the template. For every input it prints the outcome (rx-accepted, rx-rejected or tx-exception), the machine hash after the input, and the outputs Merkle root the machine declared. It also writes `run.json`, `run.log`, and the outputs and reports. It exits with 5 when a declared outputs root is wrong (see [Outputs root](#outputs-root)).

### Check a deployed application

```bash
tycho-dig check --dir work \
  --template path/to/app-template \
  --rpc https://<an Ethereum RPC, ideally not the one the node uses> \
  --node http://localhost:10011/rpc \
  --app <app name or address>
```

`check` runs three steps:

1. **fetch:** reads `InputAdded` from the InputBox up to the `finalized` block by default (`--to-block` accepts a number or a tag), writing each input's raw bytes, which are already EvmAdvance-encoded. Reading below finality would make tycho-dig itself raise false alarms on reorgs.
2. **run:** replays the inputs from the template.
3. **compare:** compares the per-input hashes with the `machine_hash` that `cartesi_listInputs` returns.

You can also run `fetch`, `run` and `compare` separately.

### Watch an application and alert

```bash
tycho-dig watch --dir watch-state \
  --template path/to/app-template --image <the emulator image the app was deployed with> \
  --rpc https://<an Ethereum RPC, ideally not the one the node uses> \
  --node http://localhost:10011/rpc --app <app name or address> \
  --interval 30 \
  --alert-webhook https://hooks.slack.com/services/... \
  --alert-cmd 'osascript -e "display notification \"$TYCHO_DIG_ALERT_TEXT\" with title \"tycho-dig\""'
```

`watch` runs until it is stopped. On every tick (`--interval` seconds) it does four things:

1. **Reads new inputs:** `InputAdded` events from the InputBox, up to the `finalized` block, through `--rpc`. The node is not involved.
2. **Advances its machine:** it gives the new inputs to the `cartesi-machine` CLI. It resumes from the machine it stored at the end of the previous tick (`--load`, then `--store`), so each input runs once. Before resuming, it checks that the stored machine starts at the hash it was stored with. On the first tick it checks the template against the contract's `getTemplateHash()`.
3. **Compares:** it compares the machine hash after every input with the node's `machine_hash`, and checks the outputs roots, the node's status and the node's lag, as `check` does.
4. **Alerts:** it sends each new alarm once, and a `RESOLVED` note when the alarm goes away. Alerts go to stdout, to `--alert-cmd` (the text arrives on stdin and in `TYCHO_DIG_ALERT_TEXT`), and to `--alert-webhook` (a JSON POST `{"text": ...}`, which Slack accepts).

| Alarm key | Fires when |
|---|---|
| `divergence:N` | the node's hash for input N differs from tycho-dig's (only the first divergent input is reported) |
| `machine-wrong:N` | the machine declared a wrong outputs root at input N |
| `outputs-root-verdict` | the node reports `INVALID_OUTPUTS_ROOT`, and the cross-check says machine wrong or node wrong |
| `status`, `disabled` | the app's status in the node is not `OK`, or the app is disabled |
| `lag` | a finalized input is still unprocessed after `--max-lag-blocks` |
| `node-template` | the node's template hash is not the contract's |
| `setup` | tycho-dig's template is not the application's; nothing is compared |
| `blocked` | an input raised an exception, so the machine takes no more inputs |
| `input-changed:N`, `chain-rewind` | an input already read below finality changed, or the finalized block went back |
| `watcher` | tycho-dig could not check for 3 ticks in a row (RPC down, CLI failing) |

Everything is kept in `--dir`, so a restarted watcher resumes where it stopped and does not resend alarms it already sent: inputs, per-input results, the outputs tree, the stored machine and the alarms. Two watchers cannot share a directory. `--once` runs one tick and exits with the code of the standing alarms, which is useful from cron.

### Replay up to input K from crisium's checkpoint

`watch` and `check` replay from input 0, so their cost grows with the app's whole history. When crisium already runs as a sentinel, its checkpoints let tycho-dig replay at most about one epoch, on demand (per epoch or per alarm), and keep no state of its own:

```bash
# crisium's data in a Docker volume: tycho-dig mounts it read-only itself
tycho-dig from-checkpoint --dir run-k5 --to 5 \
  --volume <crisium's data volume> --checkpoints /checkpoints \
  --rpc https://<an Ethereum RPC> --app <application address> \
  --image <an emulator 0.23 image with cartesi-machine> \
  --node http://localhost:10011/rpc --node-app <app name in the node>

# crisium's data on a host path, which must be mounted read-only for tycho-dig
tycho-dig from-checkpoint --dir run-epoch-2 --epoch 2 --checkpoints /crisium/checkpoints ...
```

**Requirement: crisium's data is read-only for tycho-dig.** The OS enforces this, not only tycho-dig's code. `--volume` mounts the volume `:ro` in the container that copies the checkpoint out. A host `--checkpoints` directory must be a read-only mount, for example `-v <volume>:/crisium:ro` when tycho-dig runs in a container. tycho-dig refuses a checkpoint it could write to. Only the copy in `--dir` is ever loaded, which also matters because crisium deletes old checkpoints (`--keep-epochs=1`).

**crisium's checkpoints.** crisium (machine-emulator 0.23, `doc/recipes/prt-stored-player.lua`) writes `checkpoints/epoch-N/` as soon as it has computed epoch N-1. The folder holds the machine epoch N starts from, plus `epoch.json` (`final_state_hash` is that start state) and `outputs-frontier.json` (the outputs tree since genesis).

At any moment epoch N-1 is accepted, N is sealed (its dispute runs) and N+1 is open. `DaveConsensus` seals N+1 only once N is accepted. So two kinds of checkpoint exist:

| Checkpoint | Its machine | Anchored by |
|---|---|---|
| `epoch-N` (sealed epoch) | N-1's accepted end | the chain: `EpochSealed(N).initialMachineStateHash` and outputs root |
| `epoch-(N+1)` (open epoch) | N's end as crisium computed it | crisium only, until N is accepted; cross-checked against `EpochStaged(N)` once staged, and against the node's hash for N's last input |

**Which checkpoint it starts from.** It picks the newest anchored checkpoint at or below `K`:

- **`K` in a sealed epoch:** starts from that epoch's checkpoint.
- **`K` in the open epoch:** starts from the sealed epoch before it and replays through it into the open one. That costs two epochs but stays anchored on chain.
- **`--unanchored-latest`:** starts from the open epoch's checkpoint instead. The output says the start is anchored only by crisium, and reports whether it matches `EpochStaged(N)` and the node. A disagreement refuses to run (exit 4).
- **`--epoch N`:** picks the checkpoint explicitly. Without `--to`, it replays the whole epoch.

A start hash is never reported as anchored unless it matched an on-chain value.

The command takes these steps:

1. **Chain:** reads every `EpochSealed`/`EpochStaged` of the application's `DaveConsensus`, up to `--to-block` (default `finalized`).
2. **Checkpoint check:** copies the checkpoint, then checks the copy:
   - `epoch.json` against its own frontier;
   - the start state and outputs root against the anchor (or the cross-checks);
   - the loaded machine's own state hash (`--initial-hash`, after `--clear-hash-tree` rebuilds a hash tree we did not produce).

   If any differs, it is a setup mismatch (exit 4) and nothing is compared.
3. **Inputs:** reads the inputs from the start epoch's first input to `K` from the InputBox.
4. **Replay:** runs `cartesi-machine` with `begin_epoch:true,next_input_offset:0` over the global input file indices, with `check_outputs_merkle_root:false`. It continues the outputs tree itself from `outputs-frontier.json`. The CLI would need `last_output_proof` for that, which crisium does not store.
5. **Compare:**
   - every input's outcome, machine hash and outputs root, declared and recomputed (exit 5 on a mismatch);
   - with `--node`, the node's `machine_hash` for each replayed input (exit 2);
   - for every sealed epoch the replay finished, its end state and outputs root against `EpochStaged` and the next `EpochSealed` (exit 2).

`run.json` in `--dir` keeps every check, the events, the per-input results and the timings. It needs emulator 0.23, the version that stored the checkpoint. An older CLI is refused, because it has no `begin_epoch`.

### Exit codes and alarms

`check` and `compare` exit with one of these codes:

| Exit | Meaning |
|---|---|
| 0 | All is well. |
| 2 | **Divergence:** the first input where the node's hash differs from tycho-dig's. |
| 3 | **Progress alarm:** the node stopped working on the app. |
| 4 | **Setup mismatch:** tycho-dig is not running the application's machine, so nothing was compared. |
| 5 | **Machine wrong:** the outputs root the machine declared differs from the root of the outputs it emitted. |
| 6 | **Node wrong:** the node's outputs root differs from tycho-dig's, while the machine's root agrees with it. |

When several fire, the exit code is the first of 5, 6, 2, 3. Every alarm is printed either way.

Before comparing anything, `check` verifies that the state hash of the template tycho-dig loaded (the hash at the boundary before input 0) equals the application contract's `getTemplateHash()`. `compare` checks it against the node's `template_hash` instead.

A different emulator version gives a different hash: for example, 0.23 changes the default MIMPID, so every state hash changes. A different template does too. When the hashes differ, every per-input hash would differ with no bug anywhere, so tycho-dig refuses instead of reporting a divergence. `run` prints the emulator version and the template state hash.

A progress alarm (`PROGRESS ALARM: ...`) fires in two cases:

- **The app is stopped in the node:** its `status` is anything but `OK` (`FAILED`, `DIVERGED`, `CORRUPTED`, `GUEST_EXCEPTION`, ...), or it is disabled. `FAILED` needs an operator; the others are terminal.
- **The node is behind:** a finalized input is still unprocessed more than `--max-lag-blocks` blocks after the finalized head (default 25; a negative value disables the check). This catches a node that died or hangs while its status still says `OK`.

Both checks are stateless, so `check` can run from cron or a loop without keeping anything between runs.

### Outputs root

When the guest accepts an input, it writes the root of its outputs Merkle tree into the TX buffer. The tree holds every accepted output since genesis, and it is what the epoch's claim settles on-chain. A guest that writes the wrong root makes every output of that epoch unprovable (red-team item RT-1).

`run` recomputes the root itself after every accepted input, from the outputs the machine emitted. Its tree follows libcmt: height 63, leaf `keccak256(output)`. It then compares that root with the one the machine declared. A mismatch prints `OUTPUTS ROOT MISMATCH (machine wrong)` with the input, the declared root, and the recomputed root.

The CLI has the same check built in (`check_outputs_merkle_root`, on by default). When it fires, the CLI stops with `outputs Merkle root mismatch at input N` and exits with 1. tycho-dig turns that check off and keeps replaying, so the machine hashes of later inputs can still be compared with the node's. With a CLI that cannot turn it off, tycho-dig reads the CLI's assertion as the same detection.

The Go node checks the root only when the epoch closes, in its validator. It then marks the app `INVALID_OUTPUTS_ROOT`, with both roots in the reason. `compare` reads them and runs Diego's cross-check:

| Node declared | Node calculated | tycho-dig: machine | tycho-dig: recomputed | Verdict |
|---|---|---|---|---|
| bad | good | bad | good | **machine wrong** (exit 5): escalate to the guardian |
| good | bad | good | good | **node wrong** (exit 6): fix the node, do not foreclose |

tycho-dig's "machine wrong" alarm fires on its own, at the bad input, even before the node closes the epoch. Only the root after an epoch's last accepted input reaches the chain. tycho-dig reports every input, as the CLI does.

tycho-dig shares the emulator and the template with the node, so an emulator bug would not show here. The tree code is its own, Python's `hashlib` KECCAK-256 (OpenSSL 3.2 or later) or a pure-Python fallback. It shares nothing with libcmt or the node.

## Test template

`scripts/make-echo-template.sh` builds a template from a kernel and `rootfs-tools.ext2` that match the emulator version. The template runs `ioctl-echo-loop`, which echoes every input as a voucher, a notice and a report.

```bash
scripts/make-echo-template.sh linux.bin rootfs-tools.ext2 work/echo-template
```

## Notes

- **Matching versions:** the template must have been stored by the same emulator version as `--image` (default `cartesi/machine-emulator:0.22.0`). A 0.21 template fails to load on 0.22 with `expected "archive_version" 8 (got 7)`. Even when a template loads, tycho-dig must run the version the application was deployed with, or the template guard refuses (exit 4).
- **Emulator 0.23 (pre-release `v0.23.0-test2`), not tested yet:**
  - The default MIMPID changes from 22 to 23, so state hashes differ from 0.22.
  - Stored machines use archive version 9, so 0.22 snapshots do not load.
  - The JSON-RPC protocol is 0.9.0.
  - A 0.23 tycho-dig can only check an application deployed with a 0.23 template. Against a 0.22 app it exits with 4.
  - The CLI's on-chain proof output is changing: it will also prove that the machine is in a manual yield with reason accept. tycho-dig does not use the CLI's proof files, so this does not affect it.
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

`tycho-dig check` read the inputs from the chain, not from the node. It matched the node's `machine_hash` on all eight inputs, including the rejected ones and the final `EXCEPTION`.

Two negative controls each fired at the right input and exited with 2:

- **One payload byte changed on tycho-dig side:** the first divergence was at that input, and every later input diverged too.
- **The node's recorded `machine_hash` for one input forced to a wrong value in its Postgres:** the divergence was at that input only. The node protects completed results with a trigger (`completed input result is immutable`), so the trigger had to be disabled for the test.

### Reorg below finality (F-10, A13-03)

This scenario reproduces a known finding. The node was configured to read the chain at `latest`, which is not the default and is documented as unsafe. An `evm_revert` replaced input 0 (payload `0x41414141`, "A") with another input at the same index (`0x42424242`, "B"). The node kept the orphaned input A and claimed with it. tycho-dig reads the InputBox only up to `finalized`, so it replayed the canonical input B.

| Time | Event |
|---|---|
| 15:55:38 | Input B is finalized. tycho-dig reports a divergence at input 0. |
| 16:05:20 | The node marks the app `DIVERGED` ("inconsistent commitment between off-chain and on-chain"). |

tycho-dig raised the alarm about 9 minutes 40 seconds earlier, as soon as the input was final. Before that it raised nothing, because the inputs it reads are only the final ones.

In the control run (`NODE_DEFAULT_BLOCK=finalized`, the same reorg), the node kept input B and epoch 1 was accepted. tycho-dig was polled every 20 s for about 13 minutes and agreed with the node in every round, so the reorg raised no false alarm.

### Node restarts

The setup was the tester app on rollups-node alpha.13, with snapshot policy `NONE` (the default). tycho-dig agreed with the node after every restart, which shows the node came back with the right state. The node's logs do not show that, because they do not record the replay.

| Case | What the node did | tycho-dig |
|---|---|---|
| `docker restart` of the advancer between inputs | reloaded the template, replayed, and processed the inputs that were waiting | 7/7 agree |
| `SIGKILL` while a 4.8 MB input was being processed | left the input with status `NONE` (no partial result), reloaded the template, and processed it again | 14/14 agree |

With `EVERY_INPUT`, the advancer could not write the snapshot, because the test stack points `CARTESI_SNAPSHOTS_DIR` at the read-only template mount. It destroyed the runtime, set the app to `FAILED`, and processed nothing more until an operator ran `cartesi-rollups-cli app status app enabled`. That command asks the operator to "verify snapshot/database consistency" first. `tycho-dig compare` does that check: it confirmed the processed inputs before re-enabling, and after recovery all inputs agreed. The progress alarm (exit 3) now catches both stalls:

- a `FAILED` app, at once, from its status;
- a stopped advancer whose status still said `OK`, after the lag limit. One input was final for 33 blocks against a limit of 25.

After recovery, `check` exits with 0 again.

### Outputs root mismatch (RT-1, 2026-11-09)

A test guest emitted its notices, then accepted input 1 with `0x5a...5a` in the TX buffer instead of its outputs root. The run used rollups-node `v2.0.0-alpha.13` and the `cartesi-machine` 0.21.0 from the node image. The node marked the app `INVALID_OUTPUTS_ROOT` when epoch 1 closed.

| Step | Result |
|---|---|
| `tycho-dig run` on the run's two inputs | the template matches the app's (`0xbc2ff7bd...c85f`); input 1 is flagged `OUTPUTS ROOT MISMATCH (machine wrong)`: declared `0x5a...5a`, recomputed `0xb2b7...c2e6`; exit 5 |
| `tycho-dig compare` against the node's recorded JSON-RPC answers | both machine hashes agree with the node; the node's calculated root equals ours, so the verdict is **machine wrong**; exit 5 |
| Control: the same guest without the bad root | no mismatch; exit 0 |

Before this, the CLI's own check stopped the replay with exit 1, tycho-dig crashed with a Python `RuntimeError`, and none of its alarms fired. The unit tests replay that run's files, and they also cover the "node wrong" case: a simulated node that drops the last output.

### From crisium's checkpoints (emulator 0.23, 2026-11-26)

`from-checkpoint` was run against a local devnet on the next version set:

- **Versions:** rollups-node `feature/bump-emulator-v0.23.0` (emulator 0.23.0-test4), Dave v3.0.0-alpha.8-test1 contracts and an honest sling, the echo app (3 outputs per input).
- **crisium:** a `crisium-sentinel` container (cartesi-machine 0.23.0). Its data volume was mounted `:ro` through `--volume`; root in the copying container gets "Read-only file system".
- **Inputs:** 0-1 in epoch 1, 2-6 in epoch 2, 7-8 in epoch 3.
- **Emulator:** the replay ran in the sentinel image with the entrypoint overridden.

| Run | Start | Replayed | Result |
|---|---|---|---|
| `--to 4`, epoch 2 open | `epoch-1`, anchored by `EpochSealed(1)` | 0..4 | 5/5 hashes = node; declared root = recomputed after every input |
| `--to 4 --unanchored-latest`, epoch 2 open | `epoch-2`, crisium only | 2..4 | start = node's hash after input 1; 3/3 hashes = node |
| `--to 4`, epoch 2 sealed | `epoch-2`, anchored by `EpochSealed(2)` (the same state as the run above) | 2..4 | 3/3 = node |
| `--to 8`, epoch 3 open, epoch 2 staged | `epoch-2`, anchored | 2..8 | 7/7 = node; epoch 2's end = `EpochStaged(2)` state and outputs root |
| `--epoch 2`, epoch 3 sealed | `epoch-2`, anchored | 2..6 | 5/5 = node; epoch 2's end = `EpochStaged(2)` and `EpochSealed(3)` (state and outputs root) |

Every run exited 0. Each took 4.4-7.2 s in all:

- the read-only copy of a 550 MB checkpoint through a container: 1.3-3.6 s;
- the replay, including the hash-tree rebuild and Docker start-up: 2.9-4.0 s;
- events, inputs and the node: under 0.1 s.

The negative paths are unit-tested with a fake CLI and chain (`tests/test_from_checkpoint.py`): a wrong checkpoint machine or output history (exit 4), a wrong declared root (exit 5), a node or staged state that differs (exit 2), an unanchored start that disagrees with `EpochStaged` (exit 4), a writable source, and an older emulator.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
