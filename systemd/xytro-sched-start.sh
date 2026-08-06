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

# The loader pins its maps, then (re)initializes the policy map with DEFAULT
# weights while attaching. If we load the learned policy too early it gets
# clobbered by that default write. So: wait until the scheduler is FULLY
# attached (state=enabled) before applying policy6.
for i in $(seq 1 100); do
    [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "enabled" ] && break
    sleep 0.2
done

# Let the loader finish any post-attach initialization.
sleep 1

if [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "enabled" ]; then
    echo "xytro: applying learned policy ($POLICY)..."
    "$STEER" load "$POLICY" || echo "xytro: WARN policy load failed"
    "$STEER" slice "$BASE_NS" "$FAST_MULT" || echo "xytro: WARN slice set failed"
else
    echo "xytro: WARN scheduler not enabled; running with default policy"
fi

echo "xytro: scheduler live (loader pid $LOADER_PID); state=$(cat /sys/kernel/sched_ext/state 2>/dev/null)"
wait "$LOADER_PID"
