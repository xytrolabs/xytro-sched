#!/usr/bin/env python3
"""ab.py — A/B + rollback harness for the xytro scheduler policy.

Runs as root (needs sudo for the pinned BPF maps), while xytro_sched is
running with --no-drain.

Commands:
  snapshot <file.bin>          save the current live policy
  restore  <file.bin>          hot-load a saved policy (rollback)
  record   <secs> <file.jsonl> collect a trace under the current policy
  score    <file.jsonl>        print reward metrics from a trace
  compare  <a.jsonl> <b.jsonl> compare two traces side by side

Typical A/B loop:
  sudo python3 train/ab.py snapshot train/ab_baseline.bin
  sudo python3 train/ab.py record 30 train/baseline.jsonl
  sudo python3 train/ab.py score train/baseline.jsonl
  # ... train a new policy, then:
  sudo python3 train/ab.py restore train/policy.bin
  sudo python3 train/ab.py record 30 train/candidate.jsonl
  sudo python3 train/ab.py compare train/baseline.jsonl train/candidate.jsonl
  sudo python3 train/ab.py restore train/ab_baseline.bin   # if worse
"""
import argparse
import json
import subprocess
import sys

STEER = "./tools/xytro-steer"
TOP = "./tools/xytro-top"


def run(cmd):
    subprocess.run(cmd, check=True)


def snapshot(path):
    run([STEER, "dump", path])
    print(f"snapshot -> {path}")


def restore(path):
    run([STEER, "load", path])


def record(seconds, out):
    subprocess.run(["timeout", str(int(seconds)), TOP, "--json", out])
    print(f"recorded {out}")


def load_latencies(path):
    lats = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("kind") == "running" and e.get("latency_ns"):
                lats.append(e["latency_ns"] / 1000.0)
    return lats


def pct(sorted_lats, p):
    if not sorted_lats:
        return 0.0
    idx = int(round((p / 100.0) * (len(sorted_lats) - 1)))
    return sorted_lats[idx]


def score(path):
    lats = sorted(load_latencies(path))
    n = len(lats)
    if not n:
        print(f"{path}: no latency samples")
        return
    print(f"{path}: n={n} p50={pct(lats, 50):.0f}us "
          f"p90={pct(lats, 90):.0f}us p99={pct(lats, 99):.0f}us "
          f"max={lats[-1]:.0f}us")


def compare(a, b):
    print("--- baseline ---")
    score(a)
    print("--- candidate ---")
    score(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "restore", "record",
                                    "score", "compare"])
    ap.add_argument("args", nargs="*")
    args = ap.parse_args()

    try:
        if args.cmd == "snapshot":
            snapshot(args.args[0])
        elif args.cmd == "restore":
            restore(args.args[0])
        elif args.cmd == "record":
            record(args.args[0], args.args[1])
        elif args.cmd == "score":
            score(args.args[0])
        elif args.cmd == "compare":
            compare(args.args[0], args.args[1])
    except IndexError:
        ap.print_usage()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
