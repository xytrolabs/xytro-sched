#!/bin/bash
# xytro-sched boot wrapper (runs as root via systemd).
# Starts the scheduler loader, waits for the BPF maps to be pinned, applies the
# learned policy + slice, then stays in the foreground so systemd tracks it.
set -e

BASE="${XYTRO_BASE:-/home/raf/Desktop/Linux-Xytro}"
LOADER="$BASE/bpf/xytro_sched"
STEER="$BASE/tools/xytro-steer"
POLICY="$BASE/train/policy6.bin"
BASE_NS="${XYTRO_BASE_NS:-1000000}"     # 1 ms base slice
FAST_MULT="${XYTRO_FAST_MULT:-1000}"    # fast-lane slice mult

echo "xytro: starting scheduler loader..."
"$LOADER" --no-drain &
LOADER_PID=$!

# Wait up to ~10s for the policy map pin (loader pins maps before attaching).
for i in $(seq 1 50); do
    [ -f /sys/fs/bpf/xytro_policy ] && break
    sleep 0.2
done

if [ -f /sys/fs/bpf/xytro_policy ]; then
    echo "xytro: applying learned policy ($POLICY)..."
    "$STEER" load "$POLICY" || echo "xytro: WARN policy load failed"
    "$STEER" slice "$BASE_NS" "$FAST_MULT" || echo "xytro: WARN slice set failed"
else
    echo "xytro: WARN policy map not found; running with default policy"
fi

echo "xytro: scheduler live (loader pid $LOADER_PID); state=$(cat /sys/kernel/sched_ext/state)"
wait "$LOADER_PID"
