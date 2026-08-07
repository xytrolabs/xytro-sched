#!/usr/bin/env python3
"""
xytro-agent — the autonomous brain (M3).

Observes live scheduler telemetry, builds a workload model, reasons about the
best strategy (CoT-style, human-readable), and (optionally, --live) steers the
policy within hard guardrails. Dry-run (advisory) is the DEFAULT: it observes,
reasons, and RECOMMENDS without touching the policy map.

Usage (run while xytro_sched --no-drain is attached):
  sudo python3 agent/xytro_agent.py --seconds 30              # advisory
  sudo python3 agent/xytro_agent.py --seconds 30 --live       # constrained steering
  sudo python3 agent/xytro_agent.py --seconds 20 --ab         # A/B + auto-rollback

Guardrails (always):
  - dry-run unless --live
  - policy deltas clamped to sane bounds (never blow up the scheduler)
  - skip steering if too few observations (no signal)
  - protected tasks (pid1/kthread/loader) are enforced kernel-side; agent never
    overrides them
  - one steering action per run; every decision + reason goes to the audit log
  - --ab measures before/after and auto-rolls-back on reward regression
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOP = os.path.join(ROOT, "tools", "xytro-top")
STEER = os.path.join(ROOT, "tools", "xytro-steer")

# ---- guardrail bounds -----------------------------------------------------
THRESHOLD_MIN = 50_000          # 0.05M
THRESHOLD_MAX = 50_000_000      # 50M
BASE_MIN = 250_000              # 0.25 ms
BASE_MAX = 8_000_000            # 8 ms
MULT_MIN = 500
MULT_MAX = 4000
MIN_DECISIONS = 500             # skip steering if we saw fewer decisions
AB_REGRESSION = 0.05            # 5% reward drop -> roll back

# ---- strategy table: name -> (threshold_factor, base_slice_ns, fast_mult) --
# threshold_factor is applied to the CURRENT threshold; slices in ns.
STRATEGIES = {
    # interactive: chase low latency -> lower bar, short slices
    "interactive": (0.5, 1_000_000, 1000),
    # throughput: keep long slices, neutral bar
    "throughput": (1.0, 2_000_000, 2000),
    # balanced: middle ground
    "balanced": (0.8, 1_500_000, 1500),
    # power: fewest preemptions -> longest slices
    "power": (1.2, 4_000_000, 2000),
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def run(cmd):
    """Run a command, return (rc, stdout)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def get_policy():
    """Parse `xytro-steer get` into a dict of the fields we care about."""
    rc, out = run(["sudo", "-n", STEER, "get"])
    pol = {"threshold": None, "base_slice_ns": None,
           "fast_slice_mult": None, "dry_run": None}
    if rc != 0:
        return pol, out
    for line in out.splitlines():
        s = line.strip()
        if "interactive_threshold" in s:
            pol["threshold"] = int(s.split()[-1])
        elif "base_slice_ns" in s:
            pol["base_slice_ns"] = int(s.split()[-1])
        elif "fast_slice_mult" in s:
            pol["fast_slice_mult"] = int(s.split()[-1])
        elif "dry_run" in s:
            pol["dry_run"] = int(s.split()[-1])
    return pol, out


class Metrics:
    def __init__(self):
        self.dec = 0
        self.fast = 0
        self.run = 0
        self.lats_us = []
        self.duration_s = 0.0

    @property
    def fast_ratio(self):
        return (self.fast / self.dec) if self.dec else 0.0

    @property
    def dec_per_s(self):
        return self.dec / self.duration_s if self.duration_s else 0.0

    def pct(self, q):
        if not self.lats_us:
            return None
        ls = sorted(self.lats_us)
        return ls[min(len(ls) - 1, int(round(q * (len(ls) - 1))))]


def observe(seconds, sample=50):
    """Spawn xytro-top --json, watch for `seconds`, parse telemetry."""
    fd, path = tempfile.mkstemp(prefix="xytro_agent_", suffix=".jsonl")
    os.close(fd)
    try:
        proc = subprocess.Popen(["sudo", "-n", TOP, "--json", path,
                                 "--sample", str(sample)])
        time.sleep(seconds)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # The pinned ring buffer may contain STALE events buffered before we
        # attached (up to the ring capacity). Keep only events from the final
        # `seconds` window so metrics reflect the live observation.
        return parse(path, window_s=seconds)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def parse(path, window_s=None):
    m = Metrics()
    evs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            evs.append(ev)
    if window_s and evs:
        t_max = max(ev.get("ts", 0) for ev in evs)
        t_min = t_max - int(window_s * 1e9)
        evs = [ev for ev in evs if ev.get("ts", 0) >= t_min]
    t0 = t1 = None
    for ev in evs:
        ts = ev.get("ts", 0)
        if t0 is None:
            t0 = ts
        t1 = ts
        kind = ev.get("kind")
        if kind == "decision":
            m.dec += 1
            if ev.get("lane") == 1:
                m.fast += 1
        elif kind == "running":
            m.run += 1
            lat = ev.get("latency_ns")
            if lat is not None:
                m.lats_us.append(lat / 1000.0)
    if t0 and t1:
        m.duration_s = (t1 - t0) / 1e9
    return m


def detect_phase(m):
    """Classify the observed workload into a coarse phase."""
    if m.dec < MIN_DECISIONS:
        return "insufficient-data"
    if m.dec_per_s < 200:
        return "idle/power"
    if m.fast_ratio < 0.15:
        return "batch/throughput"
    p90 = m.pct(0.90)
    if p90 is not None and p90 > 1500:
        return "interactive/latency"
    return "interactive/mixed"


def reason(m, phase, pol, strategy):
    """CoT-style reasoning -> explanation text + chosen strategy."""
    p50 = m.pct(0.50)
    p90 = m.pct(0.90)
    p99 = m.pct(0.99)
    lines = []
    lines.append("OBSERVE: %d decisions (%.1f/s), fast-lane %.1f%%, "
                 "%d wakeup-latency samples"
                 % (m.dec, m.dec_per_s, m.fast_ratio * 100, m.run))
    lines.append("  latency p50/p90/p99 = %s/%s/%s us" % (
        fmt(p50), fmt(p90), fmt(p99)))
    lines.append("  current policy: threshold=%s base=%dns mult=%d dry=%s" % (
        pol["threshold"], pol["base_slice_ns"], pol["fast_slice_mult"],
        pol["dry_run"]))
    lines.append("INFER: phase=%s" % phase)
    if phase == "insufficient-data":
        lines.append("DECIDE: hold policy, need more signal "
                     "(<%d decisions seen)" % MIN_DECISIONS)
    elif phase == "interactive/latency":
        lines.append("DECIDE: high tail latency under load -> interactive "
                     "strategy (lower bar, shorter slices to cut the tail)")
        strat = "interactive"
    elif phase == "batch/throughput":
        lines.append("DECIDE: low fast-lane use, batch-style load -> "
                     "throughput strategy (keep long slices, raise bar)")
        strat = "throughput"
    elif phase == "idle/power":
        lines.append("DECIDE: system nearly idle -> power strategy "
                     "(longest slices, fewest preemptions)")
        strat = "power"
    else:
        lines.append("DECIDE: mixed interactive load -> balanced strategy")
        strat = "balanced"
    if strategy and strategy != "auto":
        strat = strategy
        lines.append("NOTE: user overrode strategy -> %s" % strat)
    lines.append("REASON: %s selected for %s phase; target policy delta: "
                 "threshold*%.1f base=%dns mult=%d"
                 % (strat, phase, *STRATEGIES[strat]))
    return strat, "\n".join(lines)


def llm_reason(args, m, phase):
    """Ask an SLM (GGUF via llama.cpp) to choose a strategy + reason.

    Returns (strategy, reason_text) or (None, None) on any failure so the
    caller falls back to the deterministic rule-based reasoner.
    """
    import re
    import shutil
    import subprocess

    if not getattr(args, "llm", None):
        return None, None
    exe = shutil.which("llama-simple")
    if not exe:
        print("--llm set but llama-simple not found; using rule-based reasoner")
        return None, None
    summary = "decisions=%d dec/s=%.0f fast_ratio=%.2f latency_p50=%s p90=%s p99=%sus phase=%s" % (
        m.dec, m.dec_per_s, m.fast_ratio,
        fmt(m.pct(0.50)), fmt(m.pct(0.90)), fmt(m.pct(0.99)), phase)
    user_msg = (
        "You are xytro-agent, the AI steersman of a CPU scheduler. "
        "Pick exactly one strategy from {interactive, throughput, balanced, power} "
        "and give a one-sentence reason.\n"
        "Example:\n"
        "Telemetry: decisions=900 dec/s=3000 fast_ratio=0.90 latency_p50=20.0 p90=90.0 p99=1000us phase=active\n"
        "STRATEGY=interactive\n"
        "REASON=High p99 and wakeups, push more work to the fast lane.\n"
        "\n"
        "Telemetry: " + summary +
        "\nRespond with:\nSTRATEGY=<one word>\nREASON=<one short sentence>")
    # LFM2.5 (lfm2 arch) chat template: <|startoftext|><|im_start|>user\n...<|im_end|>\n<|im_start|>assistant\n
    prompt = ("<|startoftext|><|im_start|>user\n" + user_msg +
              "<|im_end|>\n<|im_start|>assistant\n")
    try:
        # llama-simple is a one-shot example binary (no interactive REPL): it
        # prints the generation to stdout and exits. New session + /dev/null
        # stdin keeps it fully detached; stderr carries logs (non-UTF-8 progress).
        r = subprocess.run(
            [exe, "-m", args.llm, "-n", "96", prompt],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
            text=True, timeout=90)
        out = r.stdout or ""
    except Exception as e:  # noqa: BLE001
        print("llm reasoner failed (%s); using rule-based reasoner" % e)
        return None, None
    # llama-simple may echo the BOS/prompt; parse only the generation after the
    # final assistant marker. LFM often numbers the option ("STRATEGY=1: Balanced")
    # and may say "Explanation:" instead of "REASON=".
    gen = out
    mark = "<|im_start|>assistant"
    idx = out.rfind(mark)
    if idx != -1:
        gen = out[idx + len(mark):]
    sm = re.search(r"STRATEGY\s*=\s*(?:\d+\s*:\s*)?(\w+)", gen, re.IGNORECASE)
    rm = re.search(r"(?:REASON|Explanation)\s*[=:]\s*(.+)", gen, re.IGNORECASE)
    if not sm:
        # prose fallback: "The correct strategy is balanced."
        wm = re.search(r"\b(interactive|throughput|balanced|power)\b", gen, re.IGNORECASE)
        strat = wm.group(1).lower() if wm else None
    else:
        strat = sm.group(1).lower()
    if strat not in STRATEGIES:
        strat = None
    reason_txt = (rm.group(1).strip().strip('"') if rm else "") or ("SLM: " + gen.strip()[:120])
    return (strat, reason_txt) if strat else (None, None)


def fmt(v):
    return "%.1f" % v if v is not None else "n/a"


def reward(m):
    """Proxy reward: low tail latency + healthy activity - scheduling churn."""
    p99 = m.pct(0.99)
    lat = 1.0 if p99 is None else max(0.0, 1.0 - p99 / 5000.0)
    thr = min(1.0, m.dec_per_s / 20000.0)
    churn = 1.0 - m.fast_ratio          # fewer preemptions is better
    return 0.5 * lat + 0.3 * thr + 0.2 * churn


def steer(pol, strat, live, audit):
    """Compute and (if live) apply the policy delta within guardrails."""
    tf, base, mult = STRATEGIES[strat]
    if pol["threshold"] is None:
        new_thr = THRESHOLD_MIN
    else:
        new_thr = int(clamp(pol["threshold"] * tf, THRESHOLD_MIN, THRESHOLD_MAX))
    new_base = int(clamp(base, BASE_MIN, BASE_MAX))
    new_mult = int(clamp(mult, MULT_MIN, MULT_MAX))

    delta = {"threshold": new_thr, "base_slice_ns": new_base,
             "fast_slice_mult": new_mult}
    if not live:
        return "recommend", delta, "dry-run: policy unchanged"
    rc1, o1 = run(["sudo", "-n", STEER, "threshold", str(new_thr)])
    rc2, o2 = run(["sudo", "-n", STEER, "slice", str(new_base), str(new_mult)])
    if rc1 != 0 or rc2 != 0:
        return "error", delta, (o1 + o2).strip()
    return "applied", delta, "threshold=%d base=%dns mult=%d" % (
        new_thr, new_base, new_mult)


def audit_entry(audit, entry):
    if not audit:
        return
    with open(audit, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_once(args):
    """One observe -> reason -> steer -> A/B cycle. Returns True on success."""
    pol, pout = get_policy()
    if pol["threshold"] is None:
        print("FATAL: cannot read policy (is xytro_sched attached + sudo warm?)")
        print(pout[:500])
        return False

    print("== xytro-agent: observing %ds (strategy=%s %s)" %
          (args.seconds, args.strategy,
           "LIVE" if args.live else "dry-run/advisory"))
    print("-- baseline observation --")
    m0 = observe(args.seconds)
    phase = detect_phase(m0)
    strat, why = reason(m0, phase, pol, args.strategy)
    # Optional SLM reasoner (GGUF via llama-simple); falls back if unavailable.
    llm_strat, llm_txt = llm_reason(args, m0, phase)
    if llm_strat:
        strat = llm_strat
        why = why + "\nSLM: " + llm_txt
        print("  (SLM reasoner) strategy=%s" % llm_strat)
    print(why)
    action, delta, note = steer(pol, strat, args.live, args.audit)

    entry = {"ts": time.time(), "phase": phase, "strategy": strat,
             "action": action, "live": args.live,
             "metrics": {"dec": m0.dec, "dec_per_s": round(m0.dec_per_s, 1),
                         "fast_ratio": round(m0.fast_ratio, 3),
                         "p50_us": m0.pct(0.50), "p90_us": m0.pct(0.90),
                         "p99_us": m0.pct(0.99)},
             "delta": delta, "reward": round(reward(m0), 3), "note": note}
    audit_entry(args.audit, entry)
    print("-- action: %s (%s) --" % (action, note))

    # A/B: measure post-steering reward and roll back on regression.
    if args.ab and args.live and action == "applied":
        print("-- A/B: measuring post-steering reward --")
        m1 = observe(args.seconds)
        r0, r1 = reward(m0), reward(m1)
        print("  reward before=%.3f after=%.3f" % (r0, r1))
        if r1 < r0 * (1 - AB_REGRESSION):
            run(["sudo", "-n", STEER, "threshold", str(pol["threshold"])])
            run(["sudo", "-n", STEER, "slice", str(pol["base_slice_ns"]),
                 str(pol["fast_slice_mult"])])
            print("  REGRESSION -> rolled back to previous policy")
            audit_entry(args.audit,
                        {"ts": time.time(), "event": "rollback",
                         "reason": "reward %0.3f < %0.3f" % (r1, r0)})
        else:
            print("  no regression -> keeping %s strategy" % strat)
    print("== cycle done; audit log: %s ==" % args.audit)
    return True


def main():
    ap = argparse.ArgumentParser(description="xytro-agent (M3 autonomous brain)")
    ap.add_argument("--seconds", type=int, default=30,
                    help="observation window (s)")
    ap.add_argument("--strategy", choices=list(STRATEGIES) + ["auto"],
                    default="auto", help="force a strategy (default: auto)")
    ap.add_argument("--live", action="store_true",
                    help="apply steering (default: dry-run/advisory)")
    ap.add_argument("--ab", action="store_true",
                    help="A/B: measure after, auto-rollback on regression")
    ap.add_argument("--audit", default=os.path.join(HERE, "audit.log"),
                    help="append-only audit log path")
    ap.add_argument("--llm", default=None, metavar="GGUF",
                    help="path to a Q3-quantized SLM (GGUF) used as the "
                         "reasoner via llama-simple; falls back to the rule-based "
                         "CoT reasoner if unset/unavailable")
    ap.add_argument("--daemon", action="store_true",
                    help="loop forever (for the systemd boot service)")
    ap.add_argument("--interval", type=int, default=120,
                    help="seconds between daemon cycles")
    args = ap.parse_args()

    if args.daemon:
        print("== xytro-agent daemon: %ds observe every %ds (live=%s ab=%s) =="
              % (args.seconds, args.interval, args.live, args.ab))
        while True:
            try:
                run_once(args)
            except Exception as e:  # noqa: BLE001
                print("cycle error: %s" % e)
            time.sleep(args.interval)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
