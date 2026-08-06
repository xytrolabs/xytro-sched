#!/usr/bin/env python3
"""
xytro-approval — DE approval prompts for KILL / START (M4 safety).

Posts a notification in the DE notification center (visibility) and opens an
interactive Approve/Deny dialog for the actual decision. COSMIC's notification
daemon does NOT render action buttons, so the clickable prompt is a zenity
dialog; if zenity is unavailable it falls back to a console prompt.

Fail-closed: any error, missing session, or timeout => DENY.
"""
import os
import subprocess
import time

TIMEOUT_S = 60
APP_ICON = "dialog-warning"

# rate-limit: don't re-post the same title within this window (seconds)
NOTIFY_MIN_INTERVAL = 10
_last_notify = {}


def notify(title, body, urgency="normal", min_interval=NOTIFY_MIN_INTERVAL):
    """Post a notification-center popup (best-effort, rate-limited)."""
    now = time.time()
    if now - _last_notify.get(title, 0) < min_interval:
        return
    _last_notify[title] = now
    try:
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "xytro",
                 title, body],
                capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _notify(action, proc, reason):
    """Best-effort notification-center popup (request visibility)."""
    summary = "Xytro wants to %s %s (pid %d)" % (action.upper(), proc.comm,
                                                 proc.pid)
    body = "Reason: %s" % reason
    notify(summary, body, urgency="critical")


CHOICES = ["Approve", "Approve and allow always",
           "Deny", "Deny and protect always"]


def _zenity_choose(action, proc, reason, timeout):
    """4-choice approval dialog. Returns (approved: bool, list_action: str|None).

    list_action is "allow" or "protect" when the user also asked to remember
    this process in the allow/protected list, else None.
    """
    try:
        text = ("Xytro wants to %s %s (pid %d)\n\n"
                "Reason: %s" % (action.upper(), proc.comm, proc.pid, reason))
        p = subprocess.run(
            ["zenity", "--list",
             "--title=Xytro: approve %s?" % action,
             "--text=" + text,
             "--column=Choose", "--hide-header",
             "--width=440", "--height=240",
             "--ok-label=Choose", "--cancel-label=Cancel"] + CHOICES,
            capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return (False, None)          # Cancel / closed -> deny
        sel = (p.stdout or "").strip()
        if sel.startswith("Approve and allow"):
            return (True, "allow")
        if sel == "Approve":
            return (True, None)
        if "protect" in sel:
            return (False, "protect")
        return (False, None)
    except subprocess.TimeoutExpired:
        return (False, None)
    except Exception:  # noqa: BLE001
        return (None, None)               # "could not ask" -> try console


def _console_ask(action, proc, reason):
    try:
        ans = input("Xytro wants to %s %s (pid %d): %s [y/N] "
                    % (action.upper(), proc.comm, proc.pid, reason))
        return ans.strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_approval(action, proc, reason, timeout=TIMEOUT_S):
    """Return (approved: bool, list_action: str|None). Never raises.

    list_action is "allow"/"protect" when the user chose to remember this
    process in the allow or protected list. Denies on any doubt.
    """
    _notify(action, proc, reason)
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        r = _zenity_choose(action, proc, reason, timeout)
        if r != (None, None):
            return r
        print("approval: zenity unavailable; using console prompt")
    else:
        print("approval: no display; using console prompt")
    return (_console_ask(action, proc, reason), None)


if __name__ == "__main__":
    import sys

    class _P:
        comm = "demo"
        pid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    act = sys.argv[1] if len(sys.argv) > 1 else "kill"
    print("result=", ask_approval(act, _P(), " ".join(sys.argv[3:])))
