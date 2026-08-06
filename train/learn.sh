#!/bin/sh
# learn.sh — turn a recorded xytro trace into a hot-loaded learned policy.
#
# Non-root steps only. The root steps (recording the trace, loading the
# resulting policy) are printed at the end.
#
# Usage:
#   ./train/learn.sh [trace.jsonl]
#
# Recommended full loop (as root + user):
#   sudo ./tools/xytro-top --json train/trace.jsonl   # record ~60-90s under CPU load
#   ./train/learn.sh train/trace.jsonl                # collect + train (this script)
#   sudo ./tools/xytro-steer load train/policy.bin    # hot-load the learned policy
set -e

TRACE="${1:-train/trace.jsonl}"

if [ ! -f "$TRACE" ]; then
    echo "no trace at $TRACE — record one first:" >&2
    echo "  sudo ./tools/xytro-top --json $TRACE   # for ~60-90s under CPU load" >&2
    exit 1
fi

echo "== labeling trace =="
python3 train/collect.py "$TRACE"
echo
echo "== learning weights =="
python3 train/train.py
echo
echo "== done. To put the learned policy live (as root): =="
echo "  sudo ./tools/xytro-steer load train/policy.bin"
