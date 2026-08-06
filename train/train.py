#!/usr/bin/env python3
"""train.py — learn the fast-lane policy from collected traces and export a
fixed-point policy.bin that xytro-steer can hot-load.

Pure-python logistic regression (no numpy).

Model:  p(fast) = sigmoid(w . f_norm + b), with f_norm = f / 1024.
In raw fixed-point terms (features 0..1024):
    score_raw = sum(weights_raw[i] * f[i])
    fast lane iff score_raw >= interactive_threshold
so  weights_raw = w  and  interactive_threshold = -b * 1024.

The learned weights are then quantized to integers (max |w_int| bounded by
--weight-scale) and written as the XYTRO_POLICY_BIN_SIZE policy.bin.

Usage:
  python3 train/train.py --data train/dataset.csv --out train/policy.bin
  sudo ./tools/xytro-steer load train/policy.bin   # hot-load into the kernel
"""
import argparse
import csv
import math
import struct
import sys

FEATS = ["f_wakeup", "f_nice", "f_kthread", "f_util",
         "f_wake_freq", "f_rqdepth", "f_bias"]
NR = len(FEATS)
BIN_SIZE = NR * 4 + 4 * 4
FEAT_SCALE = 1024.0


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def train(X, y, lr=1.0, epochs=3000, l2=1e-3):
    n = len(X)
    w = [0.0] * NR
    b = 0.0
    for _ in range(epochs):
        gw = [0.0] * NR
        gb = 0.0
        for xi, yi in zip(X, y):
            p = sigmoid(sum(w[k] * xi[k] for k in range(NR)) + b)
            err = p - yi
            for k in range(NR):
                gw[k] += err * xi[k]
            gb += err
        for k in range(NR):
            w[k] -= lr * (gw[k] / n + l2 * w[k])
        b -= lr * gb / n
    return w, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="train/dataset.csv")
    ap.add_argument("--out", default="train/policy.bin")
    ap.add_argument("--epochs", type=int, default=2000)
    ap.add_argument("--weight-scale", type=int, default=20000,
                    help="quantize so max |weight| ~ this")
    ap.add_argument("--base-slice-ns", type=int, default=2000000)
    ap.add_argument("--fast-mult", type=int, default=2000)
    ap.add_argument("--dry-run", type=int, default=0)
    args = ap.parse_args()

    X, y = [], []
    try:
        with open(args.data) as f:
            for r in csv.DictReader(f):
                X.append([float(r[k]) / FEAT_SCALE for k in FEATS])
                y.append(int(r["label"]))
    except FileNotFoundError:
        print(f"no dataset at {args.data}; run collect.py first", file=sys.stderr)
        return 1

    if not X:
        print("empty dataset; run collect.py first", file=sys.stderr)
        return 1

    w, b = train(X, y, epochs=args.epochs)

    # Quantize raw weights (w) to ints bounded by --weight-scale.
    m = max([abs(v) for v in w] + [1e-9])
    scale = max(1, int(args.weight_scale / m))
    w_int = [int(round(v * scale)) for v in w]
    threshold = int(round(-b * FEAT_SCALE * scale))

    # Evaluate on the training set using the fixed-point decision.
    n = len(y)
    npos = sum(y)
    correct = 0
    for xi, yi in zip(X, y):
        raw = sum(w_int[k] * (xi[k] * FEAT_SCALE) for k in range(NR))
        pred = 1 if raw >= threshold else 0
        correct += (pred == yi)
    acc = correct / n

    data = struct.pack("<%di" % NR, *w_int)
    data += struct.pack("<iiiI", threshold, args.base_slice_ns,
                        args.fast_mult, args.dry_run)
    assert len(data) == BIN_SIZE, (len(data), BIN_SIZE)
    with open(args.out, "wb") as f:
        f.write(data)

    print(f"samples={n} starved={npos} acc={acc:.3f} scale={scale}")
    print("weights=%s" % w_int)
    print(f"interactive_threshold={threshold} "
          f"slice={args.base_slice_ns}/{args.fast_mult} dry_run={args.dry_run}")
    print(f"wrote {args.out} ({BIN_SIZE} bytes)")
    print(f"hot-load with: sudo ./tools/xytro-steer load {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
