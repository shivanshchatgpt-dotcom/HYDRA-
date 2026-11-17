#!/usr/bin/env bash
# HYDRA Tailscale setup helper: CHECKS ONLY + PRINTED COMMANDS.
#
# This script checks the current state (Tailscale presence, login
# state, disk space, systemd unit file) and prints the exact
# commands Baji runs BY HAND. It never installs packages, never
# changes services, and never touches the network itself.
#
# The ONE exception is the explicit opt-in flag --enable-autostart:
# when Baji passes it on the laptop, the script runs ONLY
# user-scoped service commands for the HYDRA user unit (no elevated
# privileges, user unit only). Without that flag, nothing is
# executed except read-only checks.
#
# Disk safety: when free space is low, this script warns against a
# full system upgrade and only ever suggests the small, targeted
# package install. It never suggests upgrading everything while
# disk space is tight.
#
# Test override (documented, harmless): HYDRA_DISK_FREE_BYTES, when
# set to a number, is used as the free-space figure instead of
# reading the real disk. It exists so the low-disk warning can be
# tested without filling a disk.
set -euo pipefail

ENABLE_AUTOSTART=0
for arg in "$@"; do
  case "$arg" in
    --enable-autostart) ENABLE_AUTOSTART=1 ;;
    --help|-h)
      echo "Usage: scripts/setup-tailscale.sh [--enable-autostart]"
      echo "Checks Tailscale + disk + unit state and prints the setup"
      echo "commands for Baji to run by hand. With --enable-autostart"
      echo "it also enables the HYDRA user unit (user scope only)."
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: scripts/setup-tailscale.sh [--enable-autostart]" >&2
      exit 2
      ;;
  esac
done

UNIT_FILE="$HOME/.config/systemd/user/hydra.service"

echo "=== HYDRA Tailscale setup check (checks only) ==="
echo

# 1. Tailscale presence + login state (read-only).
TS_PRESENT=0
TS_IP=""
if command -v tailscale >/dev/null 2>&1; then
  TS_PRESENT=1
  echo "tailscale: found ($(command -v tailscale))"
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  if [ -n "$TS_IP" ]; then
    echo "Tailscale IP: $TS_IP (logged in)"
  else
    echo "Tailscale IP: none (not logged in, or tailscaled is down)"
  fi
else
  echo "tailscale: NOT FOUND"
fi
echo

# 2. Disk space (read-only). Low disk => warn, never suggest a
#    full upgrade; only the targeted package install is printed.
if [ -n "${HYDRA_DISK_FREE_BYTES:-}" ]; then
  FREE_BYTES="$HYDRA_DISK_FREE_BYTES"
else
  FREE_BYTES="$(python3 -c 'import shutil; print(shutil.disk_usage("/").free)' 2>/dev/null || echo 0)"
fi
FREE_GIB="$(python3 -c 'import sys; print(f"{int(sys.argv[1]) / (1024**3):.1f}")' "$FREE_BYTES" 2>/dev/null || echo "?")"
echo "Disk free (root filesystem): $FREE_GIB GiB"
LOW_DISK=0
if [ "$FREE_BYTES" -lt 5368709120 ] 2>/dev/null; then
  LOW_DISK=1
  echo "WARNING: disk space is LOW. Do NOT run a full system upgrade now."
  echo "Do NOT run: sudo pacman -Syu  (it needs much more free space)"
  echo "Free space first, then install only the small package below."
fi
echo

# 3. systemd unit file presence (read-only).
if [ -f "$UNIT_FILE" ]; then
  echo "systemd unit file: present ($UNIT_FILE)"
else
  echo "systemd unit file: NOT installed ($UNIT_FILE)"
fi
echo

# 4. Exact commands for Baji to run by hand (printed, NOT executed).
echo "=== Run these commands yourself (only the ones you still need) ==="
if [ "$TS_PRESENT" -eq 0 ]; then
  echo "  sudo pacman -S tailscale"
fi
echo "  sudo systemctl enable --now tailscaled"
echo "  sudo tailscale up"
echo "  # Then approve this laptop (and the Muse device, once) in the"
echo "  # Tailscale admin console, if asked."
echo "  tailscale ip -4   # shows this laptop's Tailscale IP"
echo "  ./scripts/pair.sh   # prints the pairing card (read-only)"
echo "  ./scripts/start-daemon.sh --bind tailscale"
echo "  # The daemon refuses to start if Tailscale is down, and never"
echo "  # binds a public interface. See docs/TAILSCALE_AUTO.md."
echo
if [ "$LOW_DISK" -eq 1 ]; then
  echo "Disk note: with space this low, install ONLY the package"
  echo "above. Do not run a full system upgrade until you have freed"
  echo "space. See docs/TAILSCALE_AUTO.md for the honest limits."
  echo
fi

# 5. Autostart: printed by default; executed ONLY with the flag.
echo "=== Autostart (opt-in) ==="
if [ "$ENABLE_AUTOSTART" -eq 1 ]; then
  if [ ! -f "$UNIT_FILE" ]; then
    echo "Unit file is not installed, so autostart was NOT enabled."
    echo "Install it yourself first:"
    echo "  mkdir -p ~/.config/systemd/user"
    echo "  cp systemd/hydra.service ~/.config/systemd/user/hydra.service"
    echo "Then re-run: ./scripts/setup-tailscale.sh --enable-autostart"
    exit 1
  fi
  echo "Enabling the HYDRA user unit (user scope only, no sudo)..."
  systemctl --user daemon-reload
  systemctl --user enable --now hydra
  echo "Done: hydra unit enabled and started."
  echo "Stop anytime with: systemctl --user stop hydra"
else
  echo "Autostart was NOT enabled (no --enable-autostart flag)."
  echo "To enable it yourself, run:"
  echo "  mkdir -p ~/.config/systemd/user"
  echo "  cp systemd/hydra.service ~/.config/systemd/user/hydra.service"
  echo "  systemctl --user daemon-reload"
  echo "  systemctl --user enable --now hydra"
  echo "Or re-run this script with --enable-autostart on the laptop."
fi
echo "=== end of setup check ==="
