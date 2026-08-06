#!/bin/bash
# Measure schbench throughput (RPS) + scheduling churn (context switches)
# of the schbench thread group. Usage: measure_cs.sh <label>
LABEL="${1:-run}"

# Count context switches across all threads of the given pid.
cs_of() {
  local pid="$1"
  awk '/^(voluntary|nonvoluntary)_ctxt_switches/ {s+=$2} END {print s+0}' \
      /proc/"$pid"/task/*/status 2>/dev/null
}

# Start schbench, wait for its main pid to appear.
schbench -m2 -t8 > /tmp/bench_${LABEL}.log 2>&1 &
SB=$!
# schbench forks; find the actual schbench main process
sleep 1
SBMAIN=$(pgrep -f '^schbench' | head -1)
[ -z "$SBMAIN" ] && SBMAIN=$SB
CS_BEFORE=$(cs_of "$SBMAIN")

# Wait for schbench to finish (default 30s runtime)
while kill -0 "$SB" 2>/dev/null; do sleep 1; done

CS_AFTER=$(cs_of "$SBMAIN")
RPS=$(grep -E "average rps|current rps" /tmp/bench_${LABEL}.log | tail -1 | awk '{print $NF}')
W99=$(grep -A4 'Wakeup Latencies' /tmp/bench_${LABEL}.log | grep '\* 99.0th' | tail -1 | awk '{print $2}')
W90=$(grep -A4 'Wakeup Latencies' /tmp/bench_${LABEL}.log | grep '90.0th' | tail -1 | awk '{print $2}')

echo "===== $LABEL ====="
echo "  RPS               : $RPS"
echo "  context switches  : $((CS_AFTER - CS_BEFORE))"
echo "  wakeup p90        : ${W90}us"
echo "  wakeup p99        : ${W99}us"
if [ "${RPS:-0}" != "0" ] && [ -n "$RPS" ]; then
  awk -v cs="$((CS_AFTER - CS_BEFORE))" -v rps="$RPS" \
      'BEGIN { printf "  cs per 1000 reqs  : %.1f\n", cs/(rps*30/1000) }'
fi
