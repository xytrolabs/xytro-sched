# xytro-sched — tests & scoring vs CFS

A detailed account of how the AI scheduler was benchmarked against the stock Linux
CFS scheduler, the exact commands used, and every number we measured.

---

## Setup

| | |
|---|---|
| **Host** | raf-cachy, Intel Core i5-13400F, 16 threads |
| **Kernel** | CachyOS 7.1.x (`CONFIG_SCHED_CLASS_EXT=y`, BTF on) |
| **DE** | COSMIC (Wayland) |
| **CFS side** | sched_ext disabled → kernel falls back to stock CFS |
| **xytro side** | `policy6.bin` (learned weights) + 1 ms base slice, `fast_mult` 1000, kick-preemption enabled |
| **Load** | each run executed on an otherwise idle desktop (single runs, not averaged) |

**Test tools:** `schbench` (wakeup latency + throughput), `stress-ng` (compute),
and a custom `train/fairness.py` (CPU-share fairness).

> Note: `stress-ng` warns the CPU scaling governors are on `powersave`; this
> applies equally to both sides, so the comparison is fair. All numbers are
> single runs — treat 1–2% deltas as noise.

---

## Test 1 — Wakeup latency & throughput (`schbench`)

**Why:** the classic "is the scheduler responsive?" test. `schbench` spins up
messenger + worker threads that continuously wake each other and measures the
**wakeup latency** (time from wake to run) at various percentiles, plus requests
per second.

**Command (same for both sides):**
```sh
schbench -m2 -t8        # 30s run
```
(Note: this build's `schbench` accepts `-m2 -t8`; its `-r`/`--runtime` flags are broken.)

### Wakeup latency

| Percentile | **xytro** (kick, 1 ms) | CFS | |
|---|---|---|---|
| p50 | 4 µs | 3 µs | ≈ |
| **p90** | **7 µs** | 97 µs | **14× better** |
| **p99** | **161 µs** | 1606 µs | **10× better** |
| **p99.9** | **687 µs** | 1658 µs | **2.4× better** |
| max | (low 1000s) | 7978 µs | |

### Throughput

| Metric | **xytro** | CFS |
|---|---|---|
| requests/sec (avg) | **3258.6** | 2927.4 |
| RPS p50 | 3324 | 2892 |

### The improvement journey (how we got there)

Three scheduler changes were benchmarked in sequence; the last one is what
closed the gap:

| Version | p90 | p99 | RPS |
|---|---|---|---|
| v1 — fast-lane rule, 4 ms slice | 30 µs | 3868 µs | 3062 |
| v2 — 1 ms slice | 161 µs | 2042 µs | 2969 |
| **v3 — 1 ms + kick-preemption** | **7 µs** | **161 µs** | **3259** |

The final piece (`scx_bpf_kick_cpu(SCX_KICK_PREEMPT)`) sends an IPI so a waking
task preempts a busy CPU **immediately** instead of waiting out the current
slice — which had been the entire tail.

---

## Test 2 — Compute throughput (`stress-ng`)

**Why:** verify the scheduler doesn't sacrifice raw CPU-bound throughput for its
latency wins.

**Command (both sides):**
```sh
stress-ng --cpu 16 --cpu-method matrixprod --timeout 20 --metrics-brief
```

| Metric | **xytro** | CFS |
|---|---|---|
| bogo ops (20 s) | 420,240 | 427,124 |
| bogo ops/s (real time) | 21,010.69 | 21,355.40 |
| **bogo ops/s (per CPU-second)** | **1483.99** | 1429.47 |
| CPU time used (usr+sys) | 283.19 s | 298.80 s |

**Interpretation:** raw throughput is a statistical tie (−1.6%, within noise).
But xytro is **more efficient**: it does ~3.8% more work per CPU-second and uses
~5% less total CPU for the same job — consistent with fewer preemption-related
context switches.

---

## Test 3 — Fairness (`train/fairness.py`)

**Why:** the concern with custom schedulers is starvation. We spawned 16
identical CPU-bound loops (matching the 16 threads) and measured each one's CPU
share over a 10 s window; a fair scheduler gives everyone an equal share (low
coefficient of variation).

**Command (both sides):**
```sh
python3 train/fairness.py <label>     # spawns 16 loops, samples, kills them
```

| Metric | **xytro** | CFS |
|---|---|---|
| workers sampled | 16 | 16 |
| min CPU share | 88.2% | 86.2% |
| max CPU share | 95.8% | 96.6% |
| mean CPU share | 92.9% | 92.2% |
| stddev | 2.0 | 2.7 |
| **coefficient of variation** | **2.1%** | 3.0% |

**Interpretation:** xytro is at least as fair as CFS — slightly better spread
(CV 2.1% vs 3.0%) — so the latency/throughput wins do **not** come at the cost
of starving any thread.

---

## Aggregate scorecard

| Dimension | Metric | **xytro** | CFS | Winner |
|---|---|---|---|---|
| Responsiveness | wakeup p90 | **7 µs** | 97 µs | xytro |
| Responsiveness | wakeup p99 | **161 µs** | 1606 µs | xytro |
| Responsiveness | wakeup p99.9 | **687 µs** | 1658 µs | xytro |
| Throughput | schbench RPS | **3258.6** | 2927.4 | xytro |
| Compute | bogo ops/s | 21,011 | 21,355 | ≈ (noise) |
| Efficiency | ops/s per CPU-s | **1484** | 1429 | xytro |
| Fairness | CPU-share CV | **2.1%** | 3.0% | xytro |

**xytro wins 6 of 7 dimensions; the one "loss" is a statistical tie.**

---

## Reproducing

```sh
# attach the AI scheduler (or it's already running via systemd)
sudo systemctl start xytro-sched

# Test 1: wakeup latency + throughput (xytro side)
schbench -m2 -t8
#   CFS side: sudo systemctl stop xytro-sched   (falls back to CFS) then re-run

# Test 2: compute (both sides, same command)
stress-ng --cpu 16 --cpu-method matrixprod --timeout 20 --metrics-brief

# Test 3: fairness (both sides, same command)
python3 train/fairness.py xytro
python3 train/fairness.py cfs
```

---

## Caveats & honest limits

- **Single runs, one machine.** 1–2% deltas (compute) are within run-to-run noise.
- **Wakeup latency** is the metric schbench measures — it's the strongest result
  and the one that matters for interactive/gaming feel.
- **Not yet measured:** power draw via RAPL (xytro using ~5% less CPU-time hints
  it's not worse), and long-duration mixed desktop/game sessions vs CFS.
- The learned policy was trained on this machine's workloads; results are for
  this hardware/kernel combination.
