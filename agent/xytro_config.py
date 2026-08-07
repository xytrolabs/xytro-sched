#!/usr/bin/env python3
"""xytro_config.py - Hyprland-style scheduler config + last-known-good restore.

The scheduler policy is configured with a flat, Hyprland-style text file
(default: ~/.config/xytro/xytro.conf, override with XYTRO_CONFIG_DIR):

    # comment
    weight.wakeup = -1949
    weight.nice   = 16299
    weight.kthread= 12181
    weight.util   = 19998
    weight.wake_freq = -3184
    weight.rqdepth   = 0
    weight.bias      = -3601
    threshold        = 11719753
    base_slice_ns    = 1000000
    fast_slice_mult  = 1000
    dry_run          = 0
    policy           = train/policy6.bin   # optional learned-policy base

Keys are case-insensitive; `weight.x` and `weights.x` both work. Every key is
optional - values override the `policy` base (a 44-byte learned .bin) or the
built-in defaults. The policy is applied atomically via `xytro-steer load`.

The config directory also holds the last-known-good restore state:
    known_good.bin      last confirmed-working 44-byte policy
    known_good.conf     config text that produced it
    history.jsonl       append-only apply/restore/promote log
    state.json          last loader exit code + counters

Restore model ("last known working config if something breaks"):
  * apply   is provisional: it first snapshots the CURRENT live policy as
            known_good, then loads the new config. If the new config later
            stalls the scheduler (kernel watchdog disables it), the boot
            script restores known_good automatically.
  * promote marks the current live policy as last-known-good (it "worked").
            The agent does this after a clean A/B cycle.
  * restore loads known_good.bin and reverts xytro.conf to known_good.conf,
            so the on-disk config matches what is actually running again.
  * The boot wrapper applies known_good whenever the previous run exited
            non-zero (stall/crash) or config validation/apply failed.

Commands:
  show                     print the live policy as config text
  validate [--file F]      parse+validate a config file (no apply)
  apply [--file F] [--bootstrap] [--promote]
                           validate, snapshot, and apply. --bootstrap skips
                           the snapshot (boot: fresh attach = loader defaults,
                           don't clobber the on-disk known-good). --promote
                           also marks the applied config as known-good.
  promote                  snapshot the current live policy as known-good
  restore                  load known-good + revert xytro.conf
  status [--broke]         print state / whether the previous run broke
  record-exit <rc>         record the loader exit code (boot wrapper)
  path <key>               print a path (dir/config/known_bin/known_conf/...)
"""
import argparse
import datetime
import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STEER = os.path.join(ROOT, "tools", "xytro-steer")

BIN_SIZE = 44
N_FEATS = 7
# Same order as bpf/intf.h XYTRO_F_* and tools/xytro-steer.c names[].
FEAT_ORDER = ["wakeup", "nice", "kthread", "util",
              "wake_freq", "rqdepth", "bias"]

# Guardrails (mirror agent/xytro_agent.py).
THRESHOLD_MIN, THRESHOLD_MAX = 50_000, 50_000_000
BASE_MIN, BASE_MAX = 250_000, 8_000_000
MULT_MIN, MULT_MAX = 500, 4000

# Built-in defaults (same as `xytro-steer reset`).
DEFAULTS = {
    "wakeup": 200, "nice": 150, "kthread": -400, "util": -100,
    "wake_freq": 150, "rqdepth": 0, "bias": 0,
    "threshold": 220_000, "base_slice_ns": 2_000_000,
    "fast_slice_mult": 2000, "dry_run": 0,
}


# --------------------------------------------------------------------------
# paths / state
# --------------------------------------------------------------------------

def config_dir():
    return (os.environ.get("XYTRO_CONFIG_DIR")
            or os.path.join(os.path.expanduser("~"), ".config", "xytro"))


def paths():
    d = config_dir()
    return {
        "dir": d,
        "config": os.path.join(d, "xytro.conf"),
        "known_bin": os.path.join(d, "known_good.bin"),
        "known_conf": os.path.join(d, "known_good.conf"),
        "history": os.path.join(d, "history.jsonl"),
        "state": os.path.join(d, "state.json"),
    }


def _read_state():
    try:
        with open(paths()["state"]) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(st):
    p = paths()["state"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, p)


def _log(entry):
    p = paths()["history"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    entry.setdefault("ts", time.time())
    try:
        with open(p, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# steer / live policy
# --------------------------------------------------------------------------

def steer(args):
    """Run xytro-steer (sudo -n when not root). Returns (rc, output)."""
    cmd = [STEER] + args
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def get_live():
    """Parse `xytro-steer get` into a flat config dict (ints)."""
    rc, out = steer(["get"])
    if rc != 0:
        raise RuntimeError("cannot read live policy "
                           "(is xytro_sched attached?):\n" + out[:300])
    d, section = {}, None
    for line in out.splitlines():
        s = line.strip()
        if s == "weights:":
            section = "w"
            continue
        if not s:
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        key, val = parts[0], parts[-1]
        if section == "w" and key in FEAT_ORDER:
            d[key] = int(val)
        elif key == "interactive_threshold":
            d["threshold"] = int(val)
        elif key == "base_slice_ns":
            d["base_slice_ns"] = int(val)
        elif key == "fast_slice_mult":
            d["fast_slice_mult"] = int(val)
        elif key == "dry_run":
            d["dry_run"] = int(val)
    for k in FEAT_ORDER + ["threshold", "base_slice_ns",
                           "fast_slice_mult", "dry_run"]:
        if k not in d:
            raise RuntimeError("could not parse live policy (missing %s)" % k)
    return d


# --------------------------------------------------------------------------
# config parsing / validation / binary format
# --------------------------------------------------------------------------

def parse_text(text):
    """Hyprland-style: `key = value`, `#` comments, case-insensitive keys."""
    cfg = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip().lower()] = v.strip()
    return cfg


def read_policy_file(path):
    """Read a 44-byte policy .bin into a flat config dict."""
    with open(path, "rb") as f:
        b = f.read(BIN_SIZE)
    if len(b) != BIN_SIZE:
        raise ValueError("policy file %s: expected %d bytes, got %d"
                         % (path, BIN_SIZE, len(b)))
    w = struct.unpack("<%di" % N_FEATS, b[:N_FEATS * 4])
    t, base, mult, dry = struct.unpack("<iiiI", b[N_FEATS * 4:])
    d = dict(zip(FEAT_ORDER, [int(x) for x in w]))
    d.update(threshold=int(t), base_slice_ns=int(base),
             fast_slice_mult=int(mult), dry_run=int(dry))
    return d


def _int(cfg, keys, default, lo=None, hi=None):
    v = default
    for k in keys:
        if k in cfg:
            v = cfg[k]
            break
    if isinstance(v, str):
        v = v.strip()
    try:
        iv = int(v)
    except (TypeError, ValueError):
        raise ValueError("invalid integer for '%s': %r" % (keys[0], v))
    if lo is not None and iv < lo:
        raise ValueError("%s=%d below minimum %d" % (keys[0], iv, lo))
    if hi is not None and iv > hi:
        raise ValueError("%s=%d above maximum %d" % (keys[0], iv, hi))
    return iv


def _float(cfg, keys, default, lo=None, hi=None):
    v = default
    for k in keys:
        if k in cfg:
            v = cfg[k]
            break
    try:
        fv = float(v)
    except (TypeError, ValueError):
        raise ValueError("invalid number for '%s': %r" % (keys[0], v))
    if lo is not None and fv < lo:
        raise ValueError("%s=%s below minimum %s" % (keys[0], v, lo))
    if hi is not None and fv > hi:
        raise ValueError("%s=%s above maximum %s" % (keys[0], v, hi))
    return fv


def load_config(path=None, cfg=None):
    """Resolve a config file into a validated flat dict (ints).

    Raises FileNotFoundError if no file exists, ValueError on bad values.
    """
    if cfg is None:
        cfg = {}
        p = path or paths()["config"]
        if not os.path.exists(p):
            raise FileNotFoundError("no config file at %s" % p)
        with open(p) as f:
            cfg = parse_text(f.read())
    # Base: the learned policy .bin, or built-in defaults.
    pol = cfg.get("policy")
    if pol:
        pol = os.path.expanduser(pol)
        if not os.path.isabs(pol):
            pol = os.path.join(ROOT, pol)
        if not os.path.exists(pol):
            raise ValueError("policy file not found: %s" % pol)
        d = read_policy_file(pol)
    else:
        d = dict(DEFAULTS)
    # Key overrides on top.
    for k in FEAT_ORDER:
        d[k] = _int(cfg, ["weight." + k, "weights." + k], d[k],
                    lo=-(2 ** 31), hi=2 ** 31 - 1)
    d["threshold"] = _int(cfg, ["threshold"], d["threshold"],
                          lo=THRESHOLD_MIN, hi=THRESHOLD_MAX)
    d["base_slice_ns"] = _int(cfg, ["base_slice_ns"], d["base_slice_ns"],
                              lo=BASE_MIN, hi=BASE_MAX)
    d["fast_slice_mult"] = _int(cfg, ["fast_slice_mult"], d["fast_slice_mult"],
                                lo=MULT_MIN, hi=MULT_MAX)
    d["dry_run"] = _int(cfg, ["dry_run"], d["dry_run"], lo=0, hi=1)
    # non-policy settings the agent reads from the same file
    d["explore_eps"] = _float(cfg, ["explore_eps"], 0.15, lo=0.0, hi=1.0)
    return d


def config_to_bin(d):
    w = struct.pack("<%di" % N_FEATS, *[d[k] for k in FEAT_ORDER])
    tail = struct.pack("<iiiI", d["threshold"], d["base_slice_ns"],
                       d["fast_slice_mult"], d["dry_run"])
    assert len(w) + len(tail) == BIN_SIZE
    return w + tail


def render_config(d, header=True):
    lines = []
    if header:
        lines += [
            "# xytro.conf - xytro-sched scheduler config (Hyprland-style)",
            "# key = value ; '#' comments ; keys are case-insensitive.",
            "# Every key is optional; it overrides the `policy` .bin or the",
            "# built-in defaults. Managed by agent/xytro_config.py.",
            "# Commands: validate | apply | promote | restore | status | show",
            "",
        ]
    for k in FEAT_ORDER:
        lines.append("weight.%-9s = %d" % (k, d[k]))
    lines += [
        "",
        "threshold       = %d" % d["threshold"],
        "base_slice_ns   = %d" % d["base_slice_ns"],
        "fast_slice_mult = %d" % d["fast_slice_mult"],
        "dry_run         = %d" % d["dry_run"],
        "",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# known-good restore operations
# --------------------------------------------------------------------------

def snapshot_known_good():
    """Promote the current LIVE policy to last-known-good (bin + conf)."""
    p = paths()
    os.makedirs(p["dir"], exist_ok=True)
    rc, out = steer(["dump", p["known_bin"]])
    if rc != 0:
        raise RuntimeError("snapshot failed: " + out.strip()[:300])
    try:
        d = get_live()
        with open(p["known_conf"], "w") as f:
            f.write(render_config(d))
    except Exception as e:  # noqa: BLE001
        _log({"event": "promote_warn", "reason": str(e)})
    st = _read_state()
    st["known_good_ts"] = time.time()
    _write_state(st)
    _log({"event": "promote", "bin": p["known_bin"]})
    return p["known_bin"]


def apply_config(path=None, bootstrap=False, promote_on_success=False):
    """Validate + apply a config file (provisional). Returns the dict."""
    p = paths()
    cfg_path = path or p["config"]
    if not os.path.exists(cfg_path):
        raise FileNotFoundError("no config file at %s" % cfg_path)
    d = load_config(cfg_path)          # raises ValueError before touching live
    if not bootstrap:
        snapshot_known_good()          # rollback point = the pre-change policy
    b = config_to_bin(d)
    os.makedirs(p["dir"], exist_ok=True)
    tmp = os.path.join(p["dir"], ".apply.bin")
    with open(tmp, "wb") as f:
        f.write(b)
    rc, out = steer(["load", tmp])
    try:
        os.unlink(tmp)
    except OSError:
        pass
    if rc != 0:
        if os.path.exists(p["known_bin"]):
            steer(["load", p["known_bin"]])
            _log({"event": "apply_failed_restored",
                  "reason": out.strip()[:300]})
        raise RuntimeError("apply failed: " + out.strip()[:300])
    st = _read_state()
    st["last_apply"] = time.time()
    st["last_apply_file"] = cfg_path
    _write_state(st)
    _log({"event": "apply", "file": cfg_path, "bootstrap": bootstrap,
          "dry_run": d["dry_run"], "threshold": d["threshold"],
          "base_slice_ns": d["base_slice_ns"],
          "fast_slice_mult": d["fast_slice_mult"]})
    if promote_on_success:
        snapshot_known_good()
    return d


def restore_known_good():
    """Load last-known-good and revert xytro.conf to known_good.conf."""
    p = paths()
    if not os.path.exists(p["known_bin"]):
        raise FileNotFoundError("no known-good policy at %s" % p["known_bin"])
    rc, out = steer(["load", p["known_bin"]])
    if rc != 0:
        raise RuntimeError("restore failed: " + out.strip()[:300])
    if os.path.exists(p["known_conf"]):
        try:
            with open(p["known_conf"]) as f:
                text = f.read()
            with open(p["config"], "w") as f:
                f.write(text)
        except OSError as e:
            _log({"event": "restore_conf_warn", "reason": str(e)})
    st = _read_state()
    st["last_restore"] = time.time()
    st["last_exit"] = 0                     # consume the "broke" flag
    st["recoveries"] = st.get("recoveries", 0) + 1
    _write_state(st)
    _log({"event": "restore", "bin": p["known_bin"]})
    return True


def record_exit(rc):
    st = _read_state()
    st["last_exit"] = int(rc)
    st["last_exit_ts"] = time.time()
    _write_state(st)
    _log({"event": "exit", "rc": int(rc)})


def _write_config_from_live():
    """Persist the current live policy into xytro.conf so a recovery rung
    survives the next boot instead of re-attempting the config that broke."""
    p = paths()
    try:
        d = get_live()
        with open(p["config"], "w") as f:
            f.write(render_config(d))
    except Exception as e:  # noqa: BLE001
        _log({"event": "recover_conf_warn", "reason": str(e)})


def record_recovery():
    """Advance the stall-recovery ladder one rung and apply that config.

    Called by the boot wrapper when the previous loader run exited non-zero
    (kernel watchdog stall / crash). xytro NEVER parks on stock CFS - each
    rung re-attaches the scheduler with a progressively safer policy:

      rung 1 (1st consecutive stall): last-known-good config (xytro.conf is
                                      also reverted to match it)
      rung 2 (2nd): built-in safe defaults (steer reset)
      rung 3 (3rd+): defaults + dry-run (normal lane only) - xytro attached
                     but inert; the absolute "not CFS" floor.

    A clean run on the next boot (reset-stalls) clears the counter so the full
    config is tried again. Returns (rung, stall_count).
    """
    p = paths()
    st = _read_state()
    n = st.get("stall_count", 0) + 1
    if n == 1 and os.path.exists(p["known_bin"]):
        restore_known_good()          # loads known-good + reverts xytro.conf
        rung = "known-good"
    else:
        if n <= 2:
            rc, out = steer(["reset"])
            if rc != 0:
                raise RuntimeError("recovery defaults failed: " +
                                   out.strip()[:200])
            rung = "defaults"
        else:
            rc, out = steer(["reset"])
            rc2, out2 = steer(["dry-run", "1"])
            if rc != 0 or rc2 != 0:
                raise RuntimeError("recovery dry-run failed: " +
                                   (out + out2).strip()[:200])
            rung = "defaults+dry-run"
        _write_config_from_live()     # persist the safe policy for next boot
        st = _read_state()
        st["last_exit"] = 0          # consume the broke flag
        st["recoveries"] = st.get("recoveries", 0) + 1
        _write_state(st)
    st = _read_state()
    st["stall_count"] = n
    _write_state(st)
    _log({"event": "recover", "rung": rung, "stall_count": n})
    return rung, n


def reset_stalls():
    st = _read_state()
    st["stall_count"] = 0
    _write_state(st)


def print_status(broke=False):
    p = paths()
    st = _read_state()
    if broke:
        print("yes" if st.get("last_exit", 0) != 0 else "no")
        return
    print("config_dir        %s" % p["dir"])
    print("config_file       %s%s" % (
        p["config"], "" if os.path.exists(p["config"]) else "   (missing)"))
    print("known_good.bin    %s" % (
        "present" if os.path.exists(p["known_bin"]) else "none"))
    ts = st.get("known_good_ts")
    if ts:
        print("known_good_at     %s" %
              datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds"))
    la = st.get("last_apply")
    print("last_apply        %s" %
          (datetime.datetime.fromtimestamp(la).isoformat(timespec="seconds")
           if la else "never"))
    print("last_loader_exit  %s" % st.get("last_exit", "n/a"))
    print("stall_count       %s" % st.get("stall_count", 0))
    print("recoveries        %s" % st.get("recoveries", 0))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="xytro scheduler config + last-known-good restore "
                    "(Hyprland-style ~/.config/xytro/xytro.conf)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("show", help="print the live policy as config text")

    a_val = sub.add_parser("validate", help="parse+validate a config (no apply)")
    a_val.add_argument("--file", default=None)

    a_apply = sub.add_parser("apply", help="validate + apply a config")
    a_apply.add_argument("--file", default=None)
    a_apply.add_argument("--bootstrap", action="store_true",
                         help="boot: skip known-good snapshot (fresh attach "
                              "writes defaults; don't clobber on-disk known-good)")
    a_apply.add_argument("--promote", action="store_true",
                         help="also mark the applied config as known-good")

    sub.add_parser("promote", help="mark the current live policy as known-good")
    sub.add_parser("restore", help="load known-good + revert xytro.conf")
    sub.add_parser("recover", help="walk the stall-recovery ladder one rung "
                  "(known-good -> defaults -> defaults+dry-run) and apply it")
    sub.add_parser("reset-stalls", help="clear the consecutive-stall counter")

    a_status = sub.add_parser("status", help="print state / break flag")
    a_status.add_argument("--broke", action="store_true",
                          help="print 'yes'/'no': did the previous run break?")

    a_rec = sub.add_parser("record-exit", help="record the loader exit code")
    a_rec.add_argument("rc")

    a_path = sub.add_parser("path", help="print a config-dir path")
    a_path.add_argument("which", choices=["dir", "config", "known_bin",
                                          "known_conf", "history", "state"])

    args = ap.parse_args(argv)
    try:
        if args.cmd == "show":
            sys.stdout.write(render_config(get_live()))
        elif args.cmd == "validate":
            d = load_config(args.file)
            sys.stdout.write("OK: %s\n" % (args.file or paths()["config"]))
            sys.stdout.write(render_config(d, header=False))
        elif args.cmd == "apply":
            d = apply_config(args.file, bootstrap=args.bootstrap,
                             promote_on_success=args.promote)
            print("applied %s (threshold=%d base=%dns mult=%d)" % (
                args.file or paths()["config"], d["threshold"],
                d["base_slice_ns"], d["fast_slice_mult"]))
        elif args.cmd == "promote":
            snapshot_known_good()
            print("promoted current live policy as last-known-good")
        elif args.cmd == "restore":
            restore_known_good()
            print("restored last-known-good")
        elif args.cmd == "recover":
            rung, n = record_recovery()
            print("recovered (rung %s, consecutive stalls %d)" % (rung, n))
        elif args.cmd == "reset-stalls":
            reset_stalls()
            print("stall counter reset")
        elif args.cmd == "status":
            print_status(broke=args.broke)
        elif args.cmd == "record-exit":
            record_exit(args.rc)
        elif args.cmd == "path":
            print(paths()[args.which])
        else:
            ap.print_help()
            return 1
    except (ValueError, RuntimeError, FileNotFoundError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
