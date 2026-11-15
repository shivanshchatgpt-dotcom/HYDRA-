#!/usr/bin/env bash
# HYDRA v0.1 Arch Linux setup (Baji's laptop: Arch + KDE Plasma + Wayland).
#
# What this script DOES:
#   * checks python3 (>= 3.11) and tailscale presence (read-only checks)
#   * creates ~/hydra-workspace and ~/.local/share/hydra
#   * generates a 0600 bearer token file if one does not exist
#   * prints exact next steps (Tailscale login, start, test)
#
# What this script NEVER does:
#   * never runs sudo / pacman -S (install guidance is printed only;
#     Baji runs it himself after reading it)
#   * never changes firewall rules
#   * never enables auto-start (systemd unit install is a separate,
#     manual, opt-in step; see README.md)
#   * never touches the network, never phones home
#
# v0.1 scope: files + shell inside the workspace only.
# NO screen control, NO GUI automation, NO Wayland input control.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${HYDRA_WORKSPACE:-$HOME/hydra-workspace}"
DATA_DIR="${HYDRA_DATA_DIR:-$HOME/.local/share/hydra}"
TOKEN_FILE="${HYDRA_TOKEN_FILE:-$DATA_DIR/token}"

echo "=== HYDRA v0.1 setup (Arch Linux) ==="
echo "Repo:      $REPO_DIR"
echo "Workspace: $WORKSPACE"
echo "Data dir:  $DATA_DIR"
echo

# 1. Python check (stdlib only, no pip packages needed).
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  echo "Install it yourself with: sudo pacman -S python"
  exit 1
fi
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "python3: $PYVER"
python3 -c 'import sys; assert sys.version_info >= (3, 11), "need Python 3.11+"' \
  || { echo "ERROR: Python 3.11+ required."; exit 1; }

# 2. Tailscale presence check (read-only; install is manual).
if command -v tailscale >/dev/null 2>&1; then
  echo "tailscale: found ($(command -v tailscale))"
  tailscale status 2>/dev/null | head -5 || true
else
  echo "tailscale: NOT FOUND (daemon still works on localhost only)."
  echo "To install it yourself, run:"
  echo "  sudo pacman -S tailscale"
  echo "Then:"
  echo "  sudo systemctl enable --now tailscaled"
  echo "  sudo tailscale up"
fi
echo

# 3. Directories.
mkdir -p "$WORKSPACE" "$DATA_DIR" "$HOME/.config/hydra"
chmod 700 "$DATA_DIR" || true
echo "Created: $WORKSPACE"
echo "Created: $DATA_DIR"

# 4. Token (0600). Never printed, never overwritten here.
if [ -f "$TOKEN_FILE" ]; then
  chmod 600 "$TOKEN_FILE"
  echo "Token file already exists (kept): $TOKEN_FILE"
else
  python3 "$REPO_DIR/scripts/generate_token.py" --token-file "$TOKEN_FILE"
fi
echo

# 5. Config example (only if user has no config yet).
if [ ! -f "$HOME/.config/hydra/config.json" ]; then
  cp "$REPO_DIR/config.example.json" "$HOME/.config/hydra/config.json"
  echo "Copied example config to ~/.config/hydra/config.json (edit if needed)."
fi

echo
echo "=== Next steps (run these yourself) ==="
echo "1) If Tailscale is installed but not logged in:"
echo "     sudo tailscale up"
echo "   Then find this laptop's Tailscale IP:"
echo "     tailscale ip -4"
echo
echo "2) Start the daemon (localhost only, for testing):"
echo "     cd $REPO_DIR"
echo "     ./scripts/start-daemon.sh"
echo
echo "3) To let Mikey reach it over Tailscale, stop it (Ctrl-C) and start:"
echo "     ./scripts/start-daemon.sh --bind <TAILSCALE-IP> --port 8765"
echo "   (Replace <TAILSCALE-IP> with the output of 'tailscale ip -4'.)"
echo
echo "4) Test from a second terminal:"
echo "     curl http://127.0.0.1:8765/health"
echo "     python3 hydra_client.py list --path ."
echo
echo "5) Stop anytime with Ctrl-C in the daemon terminal."
echo "   No auto-start is installed. See README.md for uninstall."
