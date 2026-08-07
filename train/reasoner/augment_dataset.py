#!/usr/bin/env python3
"""
augment_dataset.py — expand the SLM reasoner dataset to a large, high-quality set.

The tiny 40-example dataset hurt reasoner quality: too few examples AND the
labels used raw audit notes ("threshold=9375802 base=...") instead of natural
reasons, so the model learned to parrot config strings.

This script builds a much bigger dataset from two sources:
  1. REAL audit-log decisions (agent/audit.log) — relabeled with a natural,
     one-sentence reason per (strategy, phase), plus noisy perturbations.
  2. SYNTHETIC telemetry distilled from the rule-based reasoner's own logic
     (the same phase->strategy mapping the agent uses), so every synthetic
     label is guaranteed consistent with the deployed reasoner, and every
     regime (interactive/latency, batch/throughput, idle/power, mixed) is
     well covered.

Output (default train/reasoner/reason_dataset.jsonl), one chat JSONL object per
line, same schema as build_reason_dataset.py:
  {"messages": [system, user:"Telemetry: ...", assistant:"STRATEGY=... REASON=..."]}
"""
import argparse
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_AUDIT = os.path.join(HERE, "..", "..", "agent", "audit.log")
DEFAULT_OUT = os.path.join(HERE, "reason_dataset.jsonl")

SYSTEM = ("You are xytro-agent, the AI steersman of a CPU scheduler. "
          "Given telemetry, choose exactly one strategy from "
          "{interactive, throughput, balanced, power} and give a one-sentence "
          "reason.")

# ---- phase -> strategy mapping, mirrors agent/xytro_agent.py reason() ----
PHASE_STRATEGY = {
    "interactive/latency": "interactive",
    "batch/throughput": "throughput",
    "idle/power": "power",
    "interactive/mixed": "balanced",
}

# ---- natural, varied one-sentence reasons per strategy ----
REASONS = {
    "interactive": [
        "High tail latency under load, so I lowered the fast-lane bar and shortened slices to cut the wakeup p99.",
        "Latency-sensitive interactive load with a hot tail, so I prioritized low-latency wakeups with shorter slices.",
        "The p99 tail is climbing under load, so I reduced the bar and slices to keep wakeups snappy.",
        "Interactive workload with a rising p99, so I favor shorter slices and a lower bar.",
    ],
    "throughput": [
        "Batch-style load with little fast-lane use, so I raised the bar and kept long slices to maximize throughput.",
        "Mostly batch work that rarely needs the fast lane, so I favored long slices and a higher bar.",
        "Throughput-oriented workload, so I kept slices long and the fast-lane bar high to reduce preemption churn.",
        "Sustained compute with low wakeup sensitivity, so long slices and a high bar win.",
    ],
    "power": [
        "The system is nearly idle, so I chose the longest slices and fewest preemptions to save power.",
        "Idle/power phase, so I minimized preemption and extended slices to keep the CPU quiet.",
        "Light load detected, so I reduced scheduling churn with long slices and a high bar.",
        "Almost no active work, so long slices and minimal preemption cut energy.",
    ],
    "balanced": [
        "Mixed interactive load, so balanced slices and a moderate fast-lane bar keep latency and throughput in check.",
        "A blend of interactive and background work, so I stayed balanced on slices and bar.",
        "Moderate mixed workload, so balanced settings keep the tail and throughput both reasonable.",
        "No single signal dominates, so balanced slices and a moderate bar are safest.",
    ],
}

VALID_STRATS = tuple(PHASE_STRATEGY.values())


def prompt_for(m):
    return ("Telemetry: decisions=%d dec/s=%.1f fast_ratio=%.2f "
            "latency_p50=%s p90=%s p99=%s phase=%s" % (
                m.get("dec", 0), m.get("dec_per_s", 0.0),
                m.get("fast_ratio", 0.0),
                _fmt(m.get("p50_us")), _fmt(m.get("p90_us")),
                _fmt(m.get("p99_us")), m.get("phase", "interactive/mixed")))


def _fmt(v):
    return "%.1f" % v if v is not None else "n/a"


def make_example(m, strategy, rng):
    """One chat example with a natural reason for the given strategy."""
    reason = rng.choice(REASONS[strategy])
    reply = "STRATEGY=%s REASON=%s" % (strategy, reason)
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt_for(m)},
        {"role": "assistant", "content": reply},
    ]}


def real_examples(audit_path, rng):
    """Parse the audit log; keep usable applied entries with valid strategies,
    dropping the broken insufficient-data/dec==0 noise, and perturb each."""
    out = []
    if not os.path.exists(audit_path):
        return out
    with open(audit_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            strat = e.get("strategy")
            if strat not in VALID_STRATS:
                continue
            m = dict(e.get("metrics") or {})
            if not m.get("dec"):
                continue          # skip dec==0 (telemetry-outage artifacts)
            m["phase"] = e.get("phase", "interactive/mixed")
            out.append(make_example(m, strat, rng))
            # 4 perturbed variants per real example for more coverage
            for _ in range(4):
                pm = dict(m)
                pm["dec"] = max(500, int(m["dec"] * rng.uniform(0.5, 1.5)))
                pm["dec_per_s"] = max(0.0, m["dec_per_s"] * rng.uniform(0.5, 1.5))
                for k in ("p50_us", "p90_us", "p99_us"):
                    if m.get(k) is not None:
                        pm[k] = max(0.1, m[k] * rng.uniform(0.5, 1.8))
                out.append(make_example(pm, strat, rng))
    return out


def synth_telemetry(phase, rng):
    """Sample realistic telemetry for a phase, matching reason()'s thresholds."""
    if phase == "interactive/latency":
        return dict(dec=rng.randint(2000, 20000),
                    dec_per_s=rng.uniform(1500, 9000),
                    fast_ratio=rng.uniform(0.4, 1.0),
                    p50_us=rng.uniform(1, 8),
                    p90_us=rng.uniform(1500, 5000),
                    p99_us=rng.uniform(2000, 8000))
    if phase == "batch/throughput":
        return dict(dec=rng.randint(5000, 40000),
                    dec_per_s=rng.uniform(2000, 12000),
                    fast_ratio=rng.uniform(0.0, 0.14),
                    p50_us=rng.uniform(3, 30),
                    p90_us=rng.uniform(20, 1500),
                    p99_us=rng.uniform(50, 2000))
    if phase == "idle/power":
        return dict(dec=rng.randint(100, 400),
                    dec_per_s=rng.uniform(20, 190),
                    fast_ratio=rng.uniform(0.0, 0.5),
                    p50_us=rng.uniform(10, 100),
                    p90_us=rng.uniform(20, 500),
                    p99_us=rng.uniform(50, 1000))
    # interactive/mixed
    return dict(dec=rng.randint(2000, 30000),
                dec_per_s=rng.uniform(500, 9000),
                fast_ratio=rng.uniform(0.15, 1.0),
                p50_us=rng.uniform(0.2, 3),
                p90_us=rng.uniform(0.4, 1500),
                p99_us=rng.uniform(0.8, 2500))


def main():
    ap = argparse.ArgumentParser(description="Build a large high-quality reasoner dataset")
    ap.add_argument("--audit", default=AGENT_AUDIT)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--per-phase", type=int, default=800,
                    help="synthetic examples per phase (default 800 -> ~3200 synth)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    examples = real_examples(args.audit, rng)

    # Synthetic distillation across every phase.
    for phase in PHASE_STRATEGY:
        for _ in range(args.per_phase):
            m = synth_telemetry(phase, rng)
            m["phase"] = phase
            examples.append(make_example(m, PHASE_STRATEGY[phase], rng))

    rng.shuffle(examples)
    with open(args.out, "w") as out:
        for ex in examples:
            out.write(json.dumps(ex) + "\n")

    # quick stats
    from collections import Counter
    c = Counter(ex["messages"][2]["content"].split(" ")[0] for ex in examples)
    print("wrote %d examples -> %s" % (len(examples), args.out))
    print("strategy distribution: %s" % dict(c))


if __name__ == "__main__":
    main()
