#!/bin/bash
# Measure schbench's OWN thread-group context switches around a foreground run.
# Robust: finds the schbench pid by exact name, sums per-thread ctxt switches.
# Writes /tmp/cs_result_<label>.txt
LABEL="${1:-run}"
SBLOG=/tmp/bench_${LABEL}.log

schbench -m2 -t8 > "$SBLOG" 2>&1 &
SB=$!
# Wait until schbench's own process appears and grab its pid.
PID=""
for i in $(seq 1 60); do
    PID=$(pgrep -x schbench | head -1)
    [ -n "$PID" ] && break
    sleep 0.5
done

cs_total() {
    # sum voluntary+nonvoluntary ctxt switches across all threads of PID
    awk '/^(voluntary|nonvoluntary)_ctxt_switches/ {s+=$2} END {print s+0}' \
        /proc/"$1"/task/*/status 2>/dev/null
}

CS0=""
if [ -n "$PID" ]; then CS0=$(cs_total "$PID"); fi
wait "$SB" 2>/dev/null
CS1=""
if [ -n "$PID" ]; then CS1=$(cs_total "$PID"); fi

RPS=$(grep -E 'average rps' "$SBLOG" | awk '{print $NF}')
DELTA=$(( ${CS1:-0} - ${CS0:-0} ))
{
    echo "LABEL=$LABEL"
    echo "RPS=$RPS"
    echo "CTXT_DELTA=$DELTA"
    if [ -n "$RPS" ] && [ "$RPS" != "0" ]; then
        awk -v cs="$DELTA" -v rps="$RPS" \
            'BEGIN { printf "CTXT_PER_1KREQ=%.2f\n", cs/(rps*30/1000) }'
    fi
} > /tmp/cs_result_${LABEL}.txt
echo "wrote /tmp/cs_result_${LABEL}.txt"
