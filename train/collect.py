#!/usr/bin/env python3
"""collect.py — turn a xytro-top --json trace into the M2 training dataset.

Pairs each decision event with the task's next wakeup->run latency sample and
labels it per --label:
  tail        - the worst-latency ~20% of tasks (p80) are the positive class
  interactive - latency-critical interactive threads (high wakeup frequency,
                not a kernel thread) that were delayed beyond --lat-floor-us

Usage (as root while xytro_sched runs with --no-drain):
  sudo ./tools/xytro-top --json train/trace.jsonl   # in another terminal
  python3 train/collect.py train/trace.jsonl --out train/dataset.csv
"""
import argparse
import csv
import json
import math

FEATS = ["f_wakeup", "f_nice", "f_kthread", "f_util",
         "f_wake_freq", "f_rqdepth", "f_bias"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace", help="JSONL trace from xytro-top --json")
    ap.add_argument("--out", default="train/dataset.csv")
    ap.add_argument("--max-samples", type=int, default=20000,
                    help="cap dataset size (strided subsample if larger)")
    ap.add_argument("--label", choices=["interactive", "tail"], default="tail",
                    help="labeling objective: 'interactive' prioritizes "
                         "latency-critical interactive threads; 'tail' labels "
                         "the worst-latency tasks (p80 of observed latencies)")
    ap.add_argument("--interactive-min-wakefreq", type=int, default=512,
                    help="min f_wake_freq to count a task as interactive")
    ap.add_argument("--lat-floor-us", type=float, default=0.0,
                    help="optional extra min latency (us) for an interactive "
                         "task to be labelled; 0 = no delay requirement")
    args = ap.parse_args()

    pending = {}  # pid -> last decision event
    rows = []

    with open(args.trace) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e["kind"] == "decision":
                pending[e["pid"]] = e
            elif e["kind"] == "running" and e.get("latency_ns"):
                d = pending.pop(e["pid"], None)
                if d is None:
                    continue
                rows.append(
                    {FEATS[i]: d["feats"][i] for i in range(len(FEATS))}
                    | {"lane": d["lane"],
                       "latency_us": e["latency_ns"] / 1000.0}
                )

    # Labeling objective. 'tail' marks the worst-latency ~20% of tasks (p80),
    # which is learnable and robust. 'interactive' marks latency-critical
    # interactive threads — but note the wake_freq sensor saturates under load,
    # so 'interactive' tends to over-label on busy workloads.
    if args.label == "tail":
        lats = sorted(r["latency_us"] for r in rows)
        target = lats[int(round(0.8 * (len(lats) - 1)))] if lats else 1000.0
        for r in rows:
            r["label"] = 1 if r["latency_us"] > target else 0
    else:
        for r in rows:
            interactive = (r["f_wake_freq"] >= args.interactive_min_wakefreq and
                           r["f_kthread"] == 0)
            delayed = r["latency_us"] > args.lat_floor_us
            r["label"] = 1 if (interactive and delayed) else 0
        delayed = r["latency_us"] > args.lat_floor_us
        r["label"] = 1 if (interactive and delayed) else 0

    if len(rows) > args.max_samples:
        step = math.ceil(len(rows) / args.max_samples)
        rows = rows[::step]

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FEATS + ["lane", "latency_us", "label"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    n = len(rows)
    npos = sum(r["label"] for r in rows)
    print(f"wrote {n} samples to {args.out} "
          f"({npos} starved / {n - npos} ok)")

    if not n:
        print("hint: collect a longer trace, or lower --lat-target-us",
              file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
