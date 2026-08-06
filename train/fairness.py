#!/usr/bin/env python3
"""Fairness sampler: spawn N CPU-bound loops, measure each one's CPU share
over a window, report min/max/mean/stddev/CV. Low CV == fair scheduling.

Usage: python3 train/fairness.py <label>
Writes: /tmp/fairness_<label>.txt
"""
import os
import signal
import subprocess
import sys
import time

N = 16
WINDOW = 10.0
SPINUP = 4.0
MARKER = "xytro-fairness-spin"
CLK = os.sysconf("SC_CLK_TCK") or 100


def ticks(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            s = f.read()
        rp = s.rindex(")")
        fld = s[rp + 1:].split()
        return int(fld[11]) + int(fld[12])
    except (OSError, IndexError, ValueError):
        return None


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    procs = []
    for _ in range(N):
        p = subprocess.Popen(
            ["python3", "-c", "while True: pass", MARKER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    time.sleep(SPINUP)

    pids = [p.pid for p in procs if p.poll() is None]
    t0 = {pid: ticks(pid) for pid in pids}
    time.sleep(WINDOW)
    t1 = {pid: ticks(pid) for pid in pids}

    cpus = []
    for pid in pids:
        if t0.get(pid) is not None and t1.get(pid) is not None:
            cpus.append((t1[pid] - t0[pid]) / CLK / WINDOW * 100.0)

    for p in procs:
        try:
            os.kill(p.pid, signal.SIGKILL)
        except OSError:
            pass

    with open("/tmp/fairness_%s.txt" % label, "w") as f:
        if not cpus:
            f.write("label=%s no workers sampled\n" % label)
            return
        mean = sum(cpus) / len(cpus)
        var = sum((x - mean) ** 2 for x in cpus) / len(cpus)
        sd = var ** 0.5
        cv = (sd / mean * 100.0) if mean else 0.0
        f.write("label=%s workers=%d min=%.1f max=%.1f mean=%.1f sd=%.1f cv=%.1f%%\n"
                % (label, len(cpus), min(cpus), max(cpus), mean, sd, cv))


if __name__ == "__main__":
    main()
