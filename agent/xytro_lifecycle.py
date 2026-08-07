#!/usr/bin/env python3
"""
xytro-lifecycle — M4 lifecycle autonomy (the token protocol).

The AI watches the running system, reasons about process actions (CoT), and
enacts them under a configurable autonomy phase. Every action is rendered as a
protocol token (`<freeze>pid=…&reason=…&`), audited, and gated by hard safety
bounds (protected tasks, TOCTOU re-validation, rate limits, dry-run default).

Subcommands:
  list                       scan processes; show CPU%/RSS/nice/state, mark hogs
  watch [--seconds S]        autonomous loop: detect hogs -> reason -> act
  freeze PID / unfreeze PID  SIGSTOP / SIGCONT        (constrained+)
  prio PID DELTA             bounded nice step (±1/±2, max/min)  (constrained+)
  kill PID                   SIGTERM then SIGKILL      (full phase only)
  start CMD...               spawn a process           (full phase only)

Autonomy phases (--phase, default advisory):
  advisory      observe + reason + recommend (nothing executes)
  constrained   auto: freeze/unfreeze/prio within bounds; kill/start need --approve
  full          auto: all actions within bounds (still protected + audited)

Safety (always on):
  - protected: pid 1, kernel threads, this tool, the xytro loader/agent,
    any --protect PIDs — never frozen/killed/reniced
  - TOCTOU: /proc/<pid> re-validated immediately before acting
  - rate limit: max --max-actions per run, min interval between actions
  - bounded prio deltas; kill uses SIGTERM then grace then SIGKILL
  - dry-run unless --live; audit log append-only
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

from xytro_approval import ask_approval, notify

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_DEFAULT = os.path.join(HERE, "lifecycle_audit.log")
CONFIG_FILE = os.path.join(HERE, "xytro.xy")

CLK_TCK = os.sysconf("SC_CLK_TCK") or 100

# ---- user configuration (agent/xytro.xy) --------------------------------
# Sections: protect / lock-protect / allow / lock-allow. Entries are comm
# names or pids. lock-* entries need --force to remove. The CORE protections
# (PROTECT_COMMS, pid 1/2, xytro, shells/terminals) are hard-coded and can
# never be removed by the user.
PERSIST_PROTECT_COMMS = set()
PERSIST_PROTECT_PIDS = set()
PERSIST_LOCK_PROTECT = set()     # comms or pids-as-strings; --force to remove
PERSIST_ALLOW = set()
PERSIST_LOCK_ALLOW = set()


def _is_pid(e):
    return e.lstrip("-").isdigit()


def load_lists():
    global PERSIST_PROTECT_COMMS, PERSIST_PROTECT_PIDS, PERSIST_LOCK_PROTECT
    global PERSIST_ALLOW, PERSIST_LOCK_ALLOW
    PERSIST_PROTECT_COMMS, PERSIST_PROTECT_PIDS = set(), set()
    PERSIST_LOCK_PROTECT, PERSIST_ALLOW, PERSIST_LOCK_ALLOW = set(), set(), set()
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                entries = [e.strip() for e in val.split(",") if e.strip()]
                k = key.strip().lower()
                if k == "protect":
                    for e in entries:
                        if _is_pid(e):
                            PERSIST_PROTECT_PIDS.add(int(e))
                        else:
                            PERSIST_PROTECT_COMMS.add(e)
                elif k == "lock-protect":
                    PERSIST_LOCK_PROTECT.update(entries)
                elif k == "allow":
                    PERSIST_ALLOW.update(entries)
                elif k == "lock-allow":
                    PERSIST_LOCK_ALLOW.update(entries)
    except OSError:
        pass


def _config_text():
    prot = sorted(list(PERSIST_PROTECT_COMMS) +
                  [str(p) for p in PERSIST_PROTECT_PIDS])
    return ("# xytro.xy - xytro-sched user configuration\n"
            "# Sections: protect / lock-protect / allow / lock-allow.\n"
            "# Entries: comma-separated comm names or pids. '#' starts a comment.\n"
            "# CORE protections (init, kernel, xytro, shells/terminals) are\n"
            "# hard-coded and can never be removed; lock-* entries need --force.\n"
            "\n"
            "protect: " + ", ".join(prot) + "\n"
            "lock-protect: " + ", ".join(sorted(PERSIST_LOCK_PROTECT)) + "\n"
            "allow: " + ", ".join(sorted(PERSIST_ALLOW)) + "\n"
            "lock-allow: " + ", ".join(sorted(PERSIST_LOCK_ALLOW)) + "\n")


def save_lists():
    try:
        with open(CONFIG_FILE, "w") as f:
            f.write(_config_text())
    except OSError as e:
        print("warning: could not write %s (%s)" % (CONFIG_FILE, e))


def is_allowed(proc):
    """True if the target is auto-approved (allow or lock-allow)."""
    return (proc.comm in PERSIST_ALLOW or str(proc.pid) in PERSIST_ALLOW or
            proc.comm in PERSIST_LOCK_ALLOW or str(proc.pid) in PERSIST_LOCK_ALLOW)


def is_protected(proc, protect):
    """True if the target must never be acted on (core + user + locked)."""
    if proc is None:
        return True
    if proc.pid in protect:                       # pid 1/2, --protect, xytro loader
        return True
    if proc.comm in PROTECT_COMMS or proc.comm in PERSIST_PROTECT_COMMS:
        return True
    if proc.pid in PERSIST_PROTECT_PIDS:
        return True
    if proc.comm in PERSIST_LOCK_PROTECT or str(proc.pid) in PERSIST_LOCK_PROTECT:
        return True
    return False


def show_lists():
    print("=== xytro lists ===")
    print("\n[CORE]  hard-coded, can never be removed:")
    print("  " + (", ".join(sorted(PROTECT_COMMS)) or "(none)"))
    print("\n[protect]  never touched (user):")
    prot = sorted(list(PERSIST_PROTECT_COMMS) +
                  [str(p) for p in PERSIST_PROTECT_PIDS])
    print("  " + (", ".join(prot) or "(empty)"))
    print("\n[lock-protect]  never touched, --force to remove:")
    print("  " + (", ".join(sorted(PERSIST_LOCK_PROTECT)) or "(empty)"))
    print("\n[allow]  auto-approve kill/start (user):")
    print("  " + (", ".join(sorted(PERSIST_ALLOW)) or "(empty)"))
    print("\n[lock-allow]  auto-approve, --force to remove:")
    print("  " + (", ".join(sorted(PERSIST_LOCK_ALLOW)) or "(empty)"))
    print("\nConfig file: %s" % CONFIG_FILE)


def is_descendant(pid, root):
    """True if `pid` is `root` or a descendant of `root` (walk ppid chain)."""
    seen = set()
    while pid and pid != 1 and pid not in seen:
        if pid == root:
            return True
        seen.add(pid)
        s = read_proc(pid)
        if not s:
            break
        pid = s.get("ppid", 1)
    return False


def load_frozen():
    """pid -> freeze-timestamp registry (agent/frozen.json)."""
    try:
        with open(FROZEN_FILE) as f:
            return {int(k): float(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_frozen(reg):
    with open(FROZEN_FILE, "w") as f:
        json.dump({str(k): v for k, v in reg.items()}, f, indent=2)


def sweep_frozen(ttl, audit):
    """Auto-resume any process we froze whose TTL has expired."""
    reg = load_frozen()
    now = time.time()
    changed = False
    for pid, ts in list(reg.items()):
        if now - ts < ttl:
            continue
        st = read_proc(pid)
        if st and st["state"] == "T":
            try:
                os.kill(pid, signal.SIGCONT)
                notify("Xytro auto-resumed %s (pid %d)" % (st["comm"], pid),
                       "Freeze TTL (%ds) expired." % ttl)
                audit_entry(audit, {"ts": now, "event": "auto-unfreeze",
                                    "pid": pid, "comm": st["comm"],
                                    "ttl_s": ttl})
            except OSError:
                pass
        del reg[pid]
        changed = True
    if changed:
        save_frozen(reg)


load_lists()

HOG_CPU_PCT = 60.0          # sustained CPU% to be a "hog"
HOG_RSS_KB = 2 * 1024 * 1024  # 2 GiB RSS to be a "memory hog"
HOG_CONSEC = 4              # consecutive samples above threshold (CPU)
HOG_CONSEC_MEM = 2          # memory hogs are steady-state; 2 samples suffice
SAMPLE_GAP = 1.0            # s between samples
PRIO_MAX_STEP = 2
PRIO_MIN, PRIO_MAX = -20, 19
MAX_ACTIONS_DEFAULT = 4
ACTION_GAP = 1.0            # s between executed actions
KILL_GRACE = 3              # s between SIGTERM and SIGKILL
UNFREEZE_TTL_DEFAULT = 60   # s after which a freeze auto-resumes
FROZEN_FILE = os.path.join(HERE, "frozen.json")

# comms that are ALWAYS protected, regardless of pid
PROTECT_COMMS = {
    "systemd", "kthreadd", "kworker", "xytro_sched", "xytro-top",
    "xytro-steer", "xytro_agent", "xytro-lifecycle", "sshd", "systemd-udevd",
    "systemd-journal", "dbus-daemon", "Wayland", "Xorg", "cosmic-comp", "plasmashell",
    # interactive session: shells + terminals are never touched
    "waash", "bash", "zsh", "fish", "ksh", "dash", "sh", "mksh", "yash",
    "cosmic-term", "konsole", "gnome-terminal", "alacritty", "kitty", "foot",
    "wezterm", "xterm", "tmux", "screen", "kgx", "ptyxis", "ghostty",
    "code-oss", "electron", "cosmic-panel", "cosmic-launcher",
}


class Proc:
    __slots__ = ("pid", "comm", "cmdline", "cpu", "rss_kb", "nice",
                 "state", "protected", "samples")

    def __init__(self, pid):
        self.pid = pid
        self.comm = ""
        self.cmdline = ""
        self.cpu = 0.0
        self.rss_kb = 0
        self.nice = 0
        self.state = "?"
        self.protected = False
        self.samples = 0


def read_proc(pid):
    """Read one /proc/<pid>/stat snapshot; return a dict or None.

    Format: "pid (comm) state ppid ... utime stime ... nice ... rss". The comm
    is the only field containing parens, so split on the last ')' and parse the
    remainder from index 0.
    """
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        lp = s.index("(")
        rp = s.rindex(")")
        pid_str = s[:lp].strip()
        comm = s[lp + 1:rp]
        fld = s[rp + 1:].split()
        # after ')': state ppid pgrp session tty_nr tpgid flags minflt cminflt
        #            majflt cmajflt utime stime cutime cstime priority nice
        #            num_threads itrealvalue starttime vsize rss ...
        if not pid_str.isdigit() or len(fld) < 22:
            return None
        return {
            "pid": int(pid_str),
            "ppid": int(fld[1]),
            "comm": comm,
            "state": fld[0],
            "utime": int(fld[11]),
            "stime": int(fld[12]),
            "nice": int(fld[16]),
            "rss_pages": int(fld[21]),
        }
    except (OSError, IndexError, ValueError):
        return None


def read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\0", b" ").strip()
        return raw.decode(errors="replace")[:80]
    except OSError:
        return ""


def build_protect(args, self_pid):
    """Return a set of pids that must never be acted on."""
    prot = {1, 2, self_pid}
    try:
        for p in getattr(args, "protect", None) or []:
            prot.add(int(p))
    except (TypeError, ValueError):
        pass
    # add the xytro loader + any xytro tool processes by name
    try:
        out = subprocess.run(["pgrep", "-f", "xytro"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            pid = line.strip()
            if pid.isdigit():
                prot.add(int(pid))
    except Exception:  # noqa: BLE001
        pass
    # kernel threads have an empty cmdline; protect anything whose comm is known
    return prot


def total_cpu_ticks():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    return sum(int(t) for t in line.split()[1:])
    except (OSError, ValueError):
        pass
    return 0


def snapshot():
    """One pass over /proc -> {pid: {ticks, rss_kb, state, comm}}."""
    snap = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        s = read_proc(pid)
        if s:
            snap[pid] = s
    return snap


def scan(args, protect):
    """Two-pass CPU sampling: read all procs, wait, read again, diff."""
    s1 = snapshot()
    t1 = total_cpu_ticks()
    time.sleep(SAMPLE_GAP)
    s2 = snapshot()
    t2 = total_cpu_ticks()
    dtotal = max(1, t2 - t1)
    wall = SAMPLE_GAP
    my_sid = os.getsid(0)

    out = []
    for pid, s in s2.items():
        prev = s1.get(pid, {})
        dproc = (s["utime"] + s["stime"]) - (prev.get("utime", 0) + prev.get("stime", 0))
        frac = (dproc / CLK_TCK) / wall if wall > 0 else 0.0
        p = Proc(pid)
        p.comm = s["comm"][:15]
        p.cmdline = read_cmdline(pid)
        p.cpu = round(frac * 100.0, 1)
        p.rss_kb = s["rss_pages"] * 4
        p.nice = s["nice"]
        p.state = s["state"]
        # protected if explicitly listed, known comm, or same session as us
        # (same session == the interactive desktop/terminal world the agent
        #  runs in; we never freeze/kill those)
        same_sess = False
        try:
            same_sess = os.getsid(pid) == my_sid
        except (OSError, ValueError):
            pass
        p.protected = (is_protected(p, protect) or same_sess
                       or is_descendant(pid, os.getpid()))
        out.append(p)
    out.sort(key=lambda p: -p.cpu)
    return out


def detect_hogs(procs):
    """Return (cpu_hogs, mem_hogs): procs NOT protected that are clearly
    burning CPU or ballooning RSS."""
    cpu = [p for p in procs if not p.protected and p.cpu >= HOG_CPU_PCT]
    mem = [p for p in procs if not p.protected and p.rss_kb >= HOG_RSS_KB]
    return cpu, mem


def token_for(action, proc, reason):
    """Render the action as a protocol token (plan §5)."""
    body = f"pid={proc.pid if proc else '?'}&comm={proc.comm if proc else '?'}&reason={reason}&"
    return f"<{action}>{body}"


def reasoner(cpu_hogs, mem_hogs, phase):
    """CoT-style: pick the most sensible action from the hog lists.

    Reversible-first policy: prefer freeze/prio over kill; only consider kill
    in full phase for the worst repeat offender. Always produce a reason text.
    """
    lines = ["OBSERVE: %d CPU hogs (>=%.0f%%): %s | %d memory hogs (>=%.1f GiB): %s"
             % (len(cpu_hogs), HOG_CPU_PCT,
                ", ".join("%s(pid%d %.0f%%)" % (h.comm, h.pid, h.cpu)
                          for h in cpu_hogs[:5]) or "none",
                len(mem_hogs), HOG_RSS_KB / 1024 / 1024,
                ", ".join("%s(pid%d %.1fGiB)" % (h.comm, h.pid,
                                                 h.rss_kb / 1024 / 1024)
                          for h in sorted(mem_hogs, key=lambda p: -p.rss_kb)[:5])
                or "none")]
    if not cpu_hogs and not mem_hogs:
        lines.append("INFER: no hogs to act on; system healthy.")
        return (None, None, None), "\n".join(lines)

    if cpu_hogs:
        h = cpu_hogs[0]  # worst CPU offender
        if h.cpu >= 95 and phase == "full":
            action, verb = "kill", "kill"
            lines.append("INFER: %s pinned %.0f%% CPU (full autonomy) -> kill" %
                         (h.comm, h.cpu))
        elif h.cpu >= 85:
            action, verb = "freeze", "freeze"
            lines.append("INFER: %s pegged at %.0f%% -> freeze (reversible)" %
                         (h.comm, h.cpu))
        else:
            action, verb = "prio", "lower priority of"
            lines.append("INFER: %s busy at %.0f%% -> lower nice (bounded)" %
                         (h.comm, h.cpu))
        reason = "%s is a CPU hog (%.0f%%), %s is reversible & safe" % (
            h.comm, h.cpu, action)
    else:
        h = sorted(mem_hogs, key=lambda p: -p.rss_kb)[0]
        action, verb = "prio", "lower priority of"
        lines.append("INFER: %s using %.1f GiB RAM -> lower nice (reversible)" %
                     (h.comm, h.rss_kb / 1024 / 1024))
        reason = "%s is a memory hog (%.1f GiB), prio is reversible & safe" % (
            h.comm, h.rss_kb / 1024 / 1024)
    lines.append("DECIDE: %s pid=%d (phase=%s)" % (verb, h.pid, phase))
    lines.append("REASON: " + reason)
    lines.append("TOKEN:  " + token_for(action, h, reason))
    return (action, h, reason), "\n".join(lines)


def notify_outcome(action, proc, status, extra=""):
    """Post a notification-center popup describing an action outcome."""
    if proc is None:
        return
    name = "%s (pid %d)" % (proc.comm, proc.pid)
    verbs = {"freeze": "froze", "unfreeze": "resumed",
             "prio": "adjusted priority of", "kill": "killed",
             "start": "started"}
    v = verbs.get(action, action)
    if status.startswith("ok"):
        if action == "freeze":
            notify("Xytro froze " + name,
                   "SIGSTOP sent. Resume with <unfreeze>. " + extra)
        elif action == "unfreeze":
            notify("Xytro resumed " + name, "SIGCONT sent. " + extra)
        elif action == "prio":
            notify("Xytro adjusted priority of " + name,
                   extra or "nice value changed.")
        elif action == "kill":
            notify("Xytro killed " + name,
                   "SIGTERM/SIGKILL. " + extra, urgency="critical")
        elif action == "start":
            notify("Xytro started " + (proc.cmdline or proc.comm), extra)
        else:
            notify("Xytro %s %s" % (v, name), extra)
    elif status.startswith("denied"):
        notify("Xytro %s denied" % action,
               "You declined to %s %s." % (action, name))
    elif status.startswith("blocked"):
        notify("Xytro refused to %s %s" % (action, name),
               "%s (protected/TOCTOU rules)." % status)
    elif status.startswith("dry-run"):
        notify("Xytro (dry-run) would %s %s" % (action, name),
               "Advisory only — nothing changed.")
    else:
        notify("Xytro %s failed" % v, "%s: %s" % (name, status),
               urgency="critical")


def do_action(action, proc, reason, args, protect, audit):
    """Execute (or dry-run) one action with full guardrails. Returns status."""
    auto = False
    if is_protected(proc, protect) or proc.protected:
        notify_outcome(action, proc, "blocked:protected")
        return "blocked:protected"
    if action in ("kill", "start"):
        # KILL/START always need human approval unless the target is
        # allow-listed by the user, or --approve was given explicitly.
        if not args.approve and not is_allowed(proc):
            print("  >> asking user for approval (dialog + notification)...")
            ok, list_action = ask_approval(action, proc, reason)
            audit_entry(audit, {"ts": time.time(), "action": action,
                                "pid": proc.pid, "comm": proc.comm,
                                "phase": args.phase, "approval": ok,
                                "list_action": list_action})
            if list_action == "allow":
                PERSIST_ALLOW.add(proc.comm)
                save_lists()
                print("  >> added %s to allow list (auto-approve next time)"
                      % proc.comm)
            elif list_action == "protect":
                PERSIST_PROTECT_COMMS.add(proc.comm)
                save_lists()
                print("  >> added %s to protected list (never touched)"
                      % proc.comm)
            if not ok:
                notify_outcome(action, proc, "denied:user")
                return "denied:user"
        else:
            auto = True          # approved via --approve or allow list
    if not args.live:
        notify_outcome(action, proc, "dry-run")
        return "dry-run"

    # TOCTOU: re-validate the process still exists & is still the same comm
    cur = read_proc(proc.pid)
    if cur is None:
        notify_outcome(action, proc, "blocked:gone(toctou)")
        return "blocked:gone(toctou)"
    if cur["comm"] != proc.comm:
        notify_outcome(action, proc, "blocked:changed(toctou)")
        return "blocked:changed(toctou)"

    status = "ok"
    if action == "freeze":
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except OSError as e:
            status = "err:%s" % e
        else:
            reg = load_frozen()
            reg[proc.pid] = time.time()
            save_frozen(reg)
    elif action == "unfreeze":
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except OSError as e:
            status = "err:%s" % e
        else:
            reg = load_frozen()
            reg.pop(proc.pid, None)
            save_frozen(reg)
    elif action == "prio":
        delta = getattr(args, "delta", None)
        if delta is None:
            delta = 5           # deprioritize hogs (raise nice, no root needed)
        target = clamp_nice(proc.nice + delta)
        # renice of a foreign process needs root; use passwordless sudo when
        # not already root (sudoers drop-in from systemd/install.sh).
        cmd = ["renice", "-n", str(target), "-p", str(proc.pid)]
        if os.geteuid() != 0:
            cmd = ["sudo", "-n"] + cmd
        rc = subprocess.run(cmd, capture_output=True, text=True)
        status = "ok" if rc.returncode == 0 else "err:" + rc.stderr.strip()
    elif action == "kill":
        # SIGTERM first, then grace; only SIGKILL if it survived. If it is
        # already gone after the grace period, the SIGTERM did its job.
        # Killing a foreign process needs root; use passwordless sudo when
        # not already root (sudoers drop-in from systemd/install.sh).

        def sig(pid, sig):
            if os.geteuid() == 0:
                os.kill(pid, sig)
            else:
                subprocess.run(["sudo", "-n", "/usr/bin/kill", "-%d" % sig,
                                str(pid)], check=True, capture_output=True)

        try:
            sig(proc.pid, signal.SIGTERM)
        except Exception as e:
            status = "err:%s" % e
        else:
            time.sleep(KILL_GRACE)
            if read_proc(proc.pid) is not None:
                try:
                    sig(proc.pid, signal.SIGKILL)
                except Exception as e:
                    status = "err:%s" % e
            else:
                status = "ok"
    elif action == "start":
        sp = subprocess.Popen(args.cmd)  # noqa: S603
        status = "ok:pid=%d" % sp.pid

    audit_entry(audit, {"ts": time.time(), "action": action, "pid": proc.pid,
                        "comm": proc.comm, "reason": reason, "phase": args.phase,
                        "live": args.live, "status": status})
    extra = "Auto-approved via allow list." if auto else ""
    notify_outcome(action, proc, status, extra)
    return status


def clamp_nice(n):
    return max(PRIO_MIN, min(PRIO_MAX, n))


def audit_entry(audit, entry):
    with open(audit, "a") as f:
        f.write(json.dumps(entry) + "\n")


def find_proc(pid):
    s = read_proc(pid)
    if not s:
        return None
    p = Proc(pid)
    p.comm = s["comm"][:15]
    p.cmdline = read_cmdline(pid)
    p.cpu = 0.0
    p.rss_kb = s["rss_pages"] * 4
    p.nice = s["nice"]
    p.state = s["state"]
    return p


def cmd_list(args, protect):
    procs = scan(args, protect)
    print("%-7s %-16s %7s %8s %5s %s" % ("PID", "COMM", "CPU%", "RSS(kB)",
                                         "NI", "PROTECTED"))
    for p in procs[:args.limit]:
        print("%-7d %-16s %6.1f %8d %5d %s %s"
              % (p.pid, p.comm, p.cpu, p.rss_kb, p.nice,
                 "YES" if p.protected else "no ", p.cmdline))


def cmd_watch(args, protect):
    print("== xytro-lifecycle: watching %ds (phase=%s %s) =="
          % (args.seconds, args.phase, "LIVE" if args.live else "dry-run"))
    sweep_frozen(args.unfreeze_ttl, args.audit)   # auto-resume expired freezes
    cpu_seen = {}
    mem_seen = {}
    for _ in range(args.seconds):
        procs = scan(args, protect)
        cpus, mems = detect_hogs(procs)
        for h in cpus:
            cpu_seen.setdefault(h.pid, h).samples += 1
        for h in mems:
            mem_seen.setdefault(h.pid, h).samples += 1
        time.sleep(SAMPLE_GAP)
    cpu_hogs = [h for h in cpu_seen.values() if h.samples >= HOG_CONSEC]
    mem_hogs = [h for h in mem_seen.values() if h.samples >= HOG_CONSEC_MEM]
    (action, target, reason), text = reasoner(cpu_hogs, mem_hogs, args.phase)
    print(text)
    if action is None:
        print("-- no action --")
        return
    status = do_action(action, target, reason, args, protect, args.audit)
    print("-- action: %s pid=%d -> %s --" % (action, target.pid, status))


def cmd_lists(args):
    """Manage the persistent lists (agent/xytro.xy): protect / allow, with
    optional --lock (needs --force to remove). CORE entries are non-removable."""
    is_allow = args.cmd == "allow"
    locked = args.lock
    if args.op == "list":
        show_lists()
        return
    e = args.entry
    if not e:
        print("ERROR: %s %s needs an entry (comm or pid)" % (args.cmd, args.op))
        sys.exit(1)
    if args.op == "add":
        if is_allow:
            (PERSIST_LOCK_ALLOW if locked else PERSIST_ALLOW).add(e)
        elif _is_pid(e):
            if locked:
                PERSIST_LOCK_PROTECT.add(e)
            else:
                PERSIST_PROTECT_PIDS.add(int(e))
        else:
            (PERSIST_LOCK_PROTECT if locked else PERSIST_PROTECT_COMMS).add(e)
        save_lists()
        print("%s added to %s %s list" % (e, "locked " if locked else "",
                                           args.cmd))
        return
    # remove
    if not is_allow and e in PROTECT_COMMS:
        print("REFUSED: %s is a CORE protection and cannot be removed" % e)
        return
    if is_allow:
        if e in PERSIST_LOCK_ALLOW and not args.force:
            print("REFUSED: %s is locked; use --force to remove" % e)
            return
        PERSIST_ALLOW.discard(e)
        PERSIST_LOCK_ALLOW.discard(e)
    else:
        if e in PERSIST_LOCK_PROTECT and not args.force:
            print("REFUSED: %s is locked; use --force to remove" % e)
            return
        PERSIST_PROTECT_COMMS.discard(e)
        PERSIST_LOCK_PROTECT.discard(e)
        if _is_pid(e):
            PERSIST_PROTECT_PIDS.discard(int(e))
            PERSIST_LOCK_PROTECT.discard(e)
    save_lists()
    print("%s removed from %s list" % (e, args.cmd))


def cmd_tui(args):
    """Simple menu editor for agent/xytro.xy (needs an interactive terminal)."""
    if not sys.stdin.isatty():
        print("TUI requires an interactive terminal (run in your own terminal).")
        return
    while True:
        print("\n=== xytro.xy editor ===")
        print(" 1) Show all lists")
        print(" 2) Add to protect")
        print(" 3) Add to allow")
        print(" 4) Lock-protect an entry")
        print(" 5) Lock-allow an entry")
        print(" 6) Remove an entry")
        print(" 7) Quit")
        try:
            ch = (input("> ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ch == "1":
            show_lists()
        elif ch in ("2", "3", "4", "5"):
            target = input("  comm or pid: ").strip()
            if not target:
                continue
            if ch == "2":
                (PERSIST_PROTECT_PIDS.add(int(target)) if _is_pid(target)
                 else PERSIST_PROTECT_COMMS.add(target))
            elif ch == "3":
                PERSIST_ALLOW.add(target)
            elif ch == "4":
                PERSIST_LOCK_PROTECT.add(target)
            else:
                PERSIST_LOCK_ALLOW.add(target)
            save_lists()
            print("  added %s" % target)
        elif ch == "6":
            target = input("  comm or pid: ").strip()
            if not target:
                continue
            if target in PROTECT_COMMS:
                print("  REFUSED: %s is CORE and cannot be removed" % target)
                continue
            if (target in PERSIST_LOCK_PROTECT or
                    target in PERSIST_LOCK_ALLOW):
                ok = input("  %s is locked; remove anyway (y/N): "
                           % target).strip().lower() in ("y", "yes")
                if not ok:
                    continue
            if _is_pid(target):
                PERSIST_PROTECT_PIDS.discard(int(target))
            PERSIST_PROTECT_COMMS.discard(target)
            PERSIST_LOCK_PROTECT.discard(target)
            PERSIST_ALLOW.discard(target)
            PERSIST_LOCK_ALLOW.discard(target)
            save_lists()
            print("  removed %s" % target)
        elif ch == "7":
            return


def main():
    ap = argparse.ArgumentParser(description="xytro-lifecycle (M4 token protocol)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--phase", choices=["advisory", "constrained", "full"],
                        default="advisory")
        sp.add_argument("--live", action="store_true",
                        help="actually execute (default: dry-run)")
        sp.add_argument("--approve", action="store_true",
                        help="approve kill/start in constrained phase")
        sp.add_argument("--protect", action="append", help="extra protected pid")
        sp.add_argument("--audit", default=AUDIT_DEFAULT)

    sp = sub.add_parser("list")
    add_common(sp)
    sp.add_argument("--limit", type=int, default=25)

    sp = sub.add_parser("watch")
    add_common(sp)
    sp.add_argument("--seconds", type=int, default=15)
    sp.add_argument("--unfreeze-ttl", type=int, default=UNFREEZE_TTL_DEFAULT,
                    help="seconds after which a freeze auto-resumes (0=never)")

    for name in ("freeze", "unfreeze"):
        sp = sub.add_parser(name)
        add_common(sp)
        sp.add_argument("pid", type=int)
        sp.add_argument("--reason", default="manual request")

    sp = sub.add_parser("prio")
    add_common(sp)
    sp.add_argument("pid", type=int)
    sp.add_argument("delta", type=int, help="-2..2 nice step, or 99=min, -99=max")
    sp.add_argument("--reason", default="manual request")

    sp = sub.add_parser("kill")
    add_common(sp)
    sp.add_argument("pid", type=int)
    sp.add_argument("--reason", default="manual request")

    sp = sub.add_parser("start")
    add_common(sp)
    sp.add_argument("cmd", nargs=argparse.REMAINDER)
    sp.add_argument("--reason", default="manual start")

    # persistent user lists: allow (auto-approve) / protect (never touch).
    # --lock adds to the locked section (needs --force to remove); CORE
    # protections can never be removed.
    for name in ("allow", "protect"):
        s2 = sub.add_parser(name)
        s2.add_argument("op", choices=["add", "remove", "list"])
        s2.add_argument("entry", nargs="?", help="comm name or pid")
        s2.add_argument("--lock", action="store_true",
                        help="add to the locked section (needs --force to remove)")
        s2.add_argument("--force", action="store_true",
                        help="remove a locked entry")
        s2.add_argument("--audit", default=AUDIT_DEFAULT)

    sp = sub.add_parser("lists", help="show all lists (core + user + locked)")
    add_common(sp)

    sp = sub.add_parser("tui", help="interactive .xy editor")
    add_common(sp)

    args = ap.parse_args()
    self_pid = os.getpid()
    protect = build_protect(args, self_pid)

    if args.cmd in ("allow", "protect"):
        return cmd_lists(args)
    if args.cmd == "lists":
        show_lists()
        return
    if args.cmd == "tui":
        cmd_tui(args)
        return

    if args.cmd == "list":
        cmd_list(args, protect)
        return

    if args.cmd == "watch":
        cmd_watch(args, protect)
        return

    # direct actions
    proc = find_proc(args.pid)
    if proc is None:
        print("ERROR: pid %d not found" % args.pid)
        sys.exit(1)
    proc.protected = is_protected(proc, protect)
    if args.cmd == "prio":
        d = args.delta
        if d == 99:
            d = PRIO_MAX - proc.nice
        elif d == -99:
            d = PRIO_MIN - proc.nice
        d = max(-PRIO_MAX_STEP, min(PRIO_MAX_STEP, d))
        action = "prio"
        reason = "%s (delta %d -> nice %d)" % (args.reason, d,
                                               clamp_nice(proc.nice + d))
    else:
        action, reason = args.cmd, args.reason
    print("TOKEN: " + token_for(action, proc, reason))
    status = do_action(action, proc, reason, args, protect, args.audit)
    print("-- action: %s -> %s --" % (action, status))


if __name__ == "__main__":
    main()
