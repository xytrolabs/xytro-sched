#!/usr/bin/env python3
"""Decode a xytro policy.bin for inspection.

Usage:
  python3 train/inspect.py [policy.bin]
"""
import struct
import sys

NAMES = ["wakeup", "nice", "kthread", "util", "wake_freq", "rqdepth", "bias"]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "train/policy.bin"
    with open(path, "rb") as f:
        d = f.read()
    n = len(NAMES)
    w = struct.unpack("<%di" % n, d[: n * 4])
    t, base, mult, dry = struct.unpack("<iiiI", d[n * 4 :])
    print("policy:", path)
    for name, v in zip(NAMES, w):
        print(f"  {name:10s} {v}")
    print(f"  threshold   {t}")
    print(f"  base_slice  {base}  fast_mult {mult}  dry_run {dry}")


if __name__ == "__main__":
    main()
