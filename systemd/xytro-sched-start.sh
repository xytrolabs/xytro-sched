#!/bin/bash
# xytro-sched boot wrapper (runs as root via systemd).
# Starts the loader, waits for attach to complete, then applies the
# Hyprland-style config (agent/xytro_config.py). If the previous run broke
# (kernel watchdog stall / crash => non-zero loader exit) it restores the
# last-known-good config instead of re-applying the one that broke. Falls
# back to the trained policy6.bin if no config file exists.
set -e

BASE="${XYTRO_BASE:-/home/raf/Desktop/Linux-Xytro}"
LOADER="$BASE/bpf/xytro_sched"
CFG="$BASE/agent/xytro_config.py"
PY="${XYTRO_PY:-python3}"
STEER="$BASE/tools/xytro-steer"
POLICY="$BASE/train/policy6.bin"
BASE_NS="${XYTRO_BASE_NS:-1000000}"     # 1 ms base slice (fallback only)
FAST_MULT="${XYTRO_FAST_MULT:-1000}"    # fast-lane slice mult (fallback only)

echo "xytro: starting scheduler loader..."
"$LOADER" --no-drain &
LOADER_PID=$!

# The loader pins its maps, then (re)initializes the policy map with DEFAULT
# weights while attaching. If we apply the config too early it gets clobbered
# by that default write. So wait until the scheduler is FULLY attached
# (state=enabled) before applying anything.
for i in $(seq 1 100); do
    [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "enabled" ] && break
    sleep 0.2
done

# Let the loader finish any post-attach initialization.
sleep 1

if [ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" = "enabled" ]; then
    CONFIG="$("$PY" "$CFG" path config 2>/dev/null || true)"
    KNOWN="$("$PY" "$CFG" path known_bin 2>/dev/null || true)"

    if [ "$("$PY" "$CFG" status --broke 2>/dev/null || echo no)" = "yes" ] \
       && [ -n "$KNOWN" ] && [ -e "$KNOWN" ]; then
        # Last run was killed by the kernel watchdog (or crashed): come back
        # on the last config that actually worked, not the one that broke.
        echo "xytro: previous run broke -> restoring last-known-good config"
        "$PY" "$CFG" restore || echo "xytro: WARN known-good restore failed"
    elif [ -n "$CONFIG" ] && [ -e "$CONFIG" ]; then
        if "$PY" "$CFG" apply --bootstrap; then
            echo "xytro: applied config $CONFIG"
        elif [ -n "$KNOWN" ] && [ -e "$KNOWN" ]; then
            echo "xytro: config apply failed -> restoring last-known-good"
            "$PY" "$CFG" restore || echo "xytro: WARN known-good restore failed"
        else
            echo "xytro: WARN config apply failed (no known-good to fall back to)"
        fi
    else
        echo "xytro: no config -> falling back to trained policy"
        "$STEER" load "$POLICY" || echo "xytro: WARN policy load failed"
        "$STEER" slice "$BASE_NS" "$FAST_MULT" || echo "xytro: WARN slice set failed"
    fi
else
    echo "xytro: WARN scheduler not enabled; running with default policy"
fi

echo "xytro: scheduler live (loader pid $LOADER_PID); state=$(cat /sys/kernel/sched_ext/state 2>/dev/null)"
# Record the loader exit so the next start knows whether it broke (stall).
set +e
wait "$LOADER_PID"
RC=$?
set -e
"$PY" "$CFG" record-exit "$RC" 2>/dev/null || true
exit "$RC"
