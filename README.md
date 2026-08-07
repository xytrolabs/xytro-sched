# xytro-sched

An **AI-managed CPU scheduler** built on [sched_ext/BPF](https://www.kernel.org/doc/html/latest/scheduler/sched-ext.html) (mainline Linux 6.12+) — the scheduler runs entirely in the kernel via BPF, with an autonomous userspace brain that learns, steers, and manages processes.

No kernel patches. Load it with one command, benchmark it against CFS, and it wins on responsiveness.

## Highlights

- **In-kernel sched_ext scheduler** (`bpf/`) — a scored policy decides, per wakeup/enqueue, whether a task gets the *fast lane* (per-CPU queue + immediate preemption) or the *slow lane* (global queue).
- **Immediate wakeup preemption** (`scx_bpf_kick_cpu`) — the wakeup-latency tail that CFS wins on is eliminated.
- **Learned policy** (`train/`) — trained on real workloads; hot-loaded into the kernel map at runtime, no rebuild.
- **Autonomous agent** (`agent/xytro_agent.py`) — observes telemetry, detects the workload phase, reasons (CoT-style), picks a strategy, steers the policy within guardrails, and A/B-verifies with **automatic rollback** on regression.
- **Lifecycle autonomy** (`agent/xytro_lifecycle.py`) — the AI can freeze/unfreeze/deprioritize/kill processes, but **every kill/start requires your approval** via a notification + dialog, with persistent **allow/protect lists** and a full audit trail.
- **Safe by construction** — protected tasks (pid 1, kernel threads, the loader, your shells/session) are never touched; dry-run is the default; every action is notified and audited.

## Benchmarks vs CFS (i5-13400F, 16 threads)

| Metric | **xytro** | CFS |
|---|---|---|
| Wakeup latency p90 | **7 µs** | 97 µs |
| Wakeup latency p99 | **161 µs** | 1606 µs |
| Wakeup latency p99.9 | **687 µs** | 1658 µs |
| Throughput (schbench RPS) | **3259** | 2927 |
| Compute (stress-ng bogo ops/s) | 21,011 | 21,355 |
| Compute efficiency (ops/s per CPU-s) | **1484** | 1429 |
| Fairness (CPU-share CV, 16 workers) | **2.1%** | 3.0% |

## Repository layout

```
bpf/            sched_ext BPF scheduler + userspace loader + interface header
tools/          xytro-top (telemetry), xytro-steer (hot policy control)
train/          trace → label → train → policy.bin pipeline + A/B harness
agent/          M3 autonomous agent + M4 lifecycle manager + approval UI
refined.plan    the design document (architecture, milestones, safety)
Makefile        builds the scheduler + tools
```

## Building

```sh
# fetch the sched_ext framework headers once
git clone --depth 1 https://github.com/sched-ext/scx third_party/scx
# build (needs clang, bpftool, libbpf, gcc)
make all
```

## Running

```sh
# attach the scheduler (root); Ctrl+C to detach back to CFS
sudo ./bpf/xytro_sched --no-drain

# in another terminal: hot-load a learned policy and tune it
sudo ./tools/xytro-steer load train/policy6.bin
sudo ./tools/xytro-steer slice 1000000 1000   # base_ns fast_mult

# watch live telemetry
sudo ./tools/xytro-top

# autonomous agent: observe → reason → steer → A/B → auto-rollback
sudo python3 agent/xytro_agent.py --seconds 30 --live --ab

# lifecycle: let the AI manage processes (asks your approval for kills)
python3 agent/xytro_lifecycle.py watch --seconds 30 --phase constrained --live

# manage the protected/allowed process lists (see agent/xytro.xytro)
python3 agent/xytro_lifecycle.py lists                          # show all lists
python3 agent/xytro_lifecycle.py tui                           # interactive editor
python3 agent/xytro_lifecycle.py protect add steam --lock       # protect + lock
python3 agent/xytro_lifecycle.py protect remove steam           # --force to remove a lock
python3 agent/xytro_lifecycle.py allow add my-service           # auto-approve kill/start
```

Requires a kernel with `CONFIG_SCHED_CLASS_EXT=y` and BTF (CachyOS, and any 6.12+ mainline distro kernel have it).

## Auto-start on boot (systemd)

Make xytro the default scheduler that boots with your machine, plus the autonomous agent as a daemon:

```sh
sudo bash systemd/install.sh
```

This installs two units:

- **`xytro-sched.service`** — attaches the scheduler at boot, hot-loads `train/policy6.bin`, sets the 1 ms slice, and restarts on failure. Detaching (falling back to CFS) on shutdown is automatic.
- **`xytro-agent.service`** — runs the M3 agent as a daemon (observe → reason → steer → A/B → auto-rollback, every 2 minutes), so the policy continuously adapts to your workload with a built-in safety net.

Control them like any service:

```sh
systemctl status xytro-sched xytro-agent
sudo systemctl restart xytro-sched
sudo systemctl disable --now xytro-agent xytro-sched   # back to stock CFS
```

The unit files use `/home/raf/Desktop/Linux-Xytro` — edit `systemd/*.service` / the `XYTRO_BASE` env if you installed the tree elsewhere.

## Safety model

1. **Kernel** protects pid 1, kernel threads, and the loader.
2. **Agent** protects shells, terminals, your interactive session, your `protect` list, and a **hard-coded CORE list** (init, kernel threads, xytro's own processes, your DE/shells — these can *never* be removed, even with `--force`).
3. **You** gate every kill/start through the approval dialog, or pre-authorize via the `allow` list. Locked entries (`--lock`) need `--force` to remove, so a stray `remove` can't accidentally unprotect your important services.
4. Every decision and action is **notified to you** and written to the **audit log**.
5. Policy changes are **A/B verified** and **auto-rolled-back** on regression.

Per-machine process lists live in `agent/xytro.xytro` (gitignored; copy the committed `agent/xytro.xytro.example` to create one). Sections: `protect` / `lock-protect` / `allow` / `lock-allow`, comma-separated comm names or pids, `#` comments. The lifecycle service automatically reads it each cycle.

## Design

See [`refined.plan`](refined.plan) for the full architecture: the scored fast/slow-lane policy model, the training pipeline, the reward-driven autonomous agent, the token-based lifecycle protocol, and the phased autonomy roadmap (advisory → constrained → full).

For a plain-English account of what was built and the benchmark results vs CFS, see [`EXPLANATION.md`](EXPLANATION.md). For the full test methodology, commands, and every measured number, see [`BENCHMARKS.md`](BENCHMARKS.md).
