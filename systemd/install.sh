#!/bin/bash
# Install xytro scheduler + agent as boot services.
# Run as root (or with sudo). Safe to re-run.
set -e

BASE="${XYTRO_BASE:-/home/raf/Desktop/Linux-Xytro}"
UNITDIR=/etc/systemd/system

if [ "$(id -u)" != "0" ]; then
    echo "Please run as root: sudo bash systemd/install.sh"
    exit 1
fi

# sanity: the loader binary must exist
if [ ! -x "$BASE/bpf/xytro_sched" ]; then
    echo "ERROR: $BASE/bpf/xytro_sched not found. Build first (make all)."
    exit 1
fi

chmod +x "$BASE/systemd/xytro-sched-start.sh"

# Seed the Hyprland-style config dir for the desktop user if it's missing.
CFG_USER="${XYTRO_CFG_USER:-raf}"
CFG_DIR="/home/$CFG_USER/.config/xytro"
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/xytro.conf" ]; then
    cp "$BASE/config/xytro.conf.example" "$CFG_DIR/xytro.conf"
    echo "created $CFG_DIR/xytro.conf from example"
fi
chown -R "$CFG_USER":"$CFG_USER" "$CFG_DIR" 2>/dev/null || true
chmod -R u+rw "$CFG_DIR"

echo "== installing units =="
cp "$BASE/systemd/xytro-sched.service" "$UNITDIR/"
cp "$BASE/systemd/xytro-agent.service" "$UNITDIR/"
systemctl daemon-reload

echo "== installing passwordless sudoers drop-in (agent tools) =="
install -m 440 -o root -g root "$BASE/systemd/sudoers.xytro" /etc/sudoers.d/xytro
visudo -cf /etc/sudoers.d/xytro || { echo "sudoers syntax ERROR — aborting"; exit 1; }

echo "== enabling + starting =="
systemctl enable --now xytro-sched.service
systemctl enable --now xytro-agent.service

echo "== status =="
sleep 3
systemctl --no-pager --lines=12 status xytro-sched.service
echo
systemctl --no-pager --lines=8 status xytro-agent.service
echo
echo "Scheduler state: $(cat /sys/kernel/sched_ext/state 2>/dev/null || echo 'unknown')"
echo
echo "Done. Disable with:"
echo "  sudo systemctl disable --now xytro-agent.service xytro-sched.service"
