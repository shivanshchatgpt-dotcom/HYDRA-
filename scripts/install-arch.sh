#!/usr/bin/env bash
# HYDRA v0.2 Arch Linux setup (Baji's laptop: Arch + KDE Plasma + Wayland).
#
# What this script DOES:
#   * checks python3 (>= 3.11) and tailscale presence (read-only checks)
#   * checks the OPTIONAL v0.2 tools (ripgrep, bubblewrap, ydotool,
#     spectacle/grim) and prints install guidance for the missing ones
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
# v0.2 scope: files + shell + patch edits + search + background jobs
# inside the workspace, optional bubblewrap sandbox, and best-effort
# GUI endpoints (screenshot + input) when the tools below are present.
# NO browser control (future work, see docs/ARCHITECTURE.md).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${HYDRA_WORKSPACE:-$HOME/hydra-workspace}"
DATA_DIR="${HYDRA_DATA_DIR:-$HOME/.local/share/hydra}"
TOKEN_FILE="${HYDRA_TOKEN_FILE:-$DATA_DIR/token}"

echo "=== HYDRA v0.2 setup (Arch Linux) ==="
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

# 3. Optional v0.2 tools (read-only checks; install guidance only).
# The daemon works without all of these; each missing tool only
# disables the endpoints that need it, with a clear 501 error.
echo "Optional v0.2 tools:"
if command -v rg >/dev/null 2>&1; then
  echo "  ripgrep (rg): found — /search uses it (fast path)"
else
  echo "  ripgrep (rg): NOT FOUND — /search falls back to grep (slower)."
  echo "    To install it yourself: sudo pacman -S ripgrep"
fi
if command -v bwrap >/dev/null 2>&1; then
  echo "  bubblewrap (bwrap): found — sandboxed exec available"
else
  echo "  bubblewrap (bwrap): NOT FOUND — sandboxed exec returns 501."
  echo "    To install it yourself: sudo pacman -S bubblewrap"
  echo "    (Needs unprivileged user namespaces; stock Arch kernels allow them.)"
fi
if command -v ydotool >/dev/null 2>&1; then
  echo "  ydotool: found — /input endpoints available (needs ydotoold running)"
  echo "    Start it yourself with: sudo systemctl enable --now ydotoold"
  echo "    And add your user to the input group (one-time):"
  echo "      sudo usermod -aG input \$USER   # then log out and back in"
else
  echo "  ydotool: NOT FOUND — /input endpoints return 501."
  echo "    To install it yourself: sudo pacman -S ydotool"
fi
if command -v spectacle >/dev/null 2>&1; then
  echo "  spectacle: found — /screen/shot primary backend"
elif command -v grim >/dev/null 2>&1; then
  echo "  grim: found — /screen/shot fallback backend"
else
  echo "  spectacle/grim: NOT FOUND — /screen/shot tries the xdg-desktop-portal"
  echo "    path instead (needs a permission dialog the first time)."
  echo "    To install spectacle yourself: sudo pacman -S spectacle"
fi
echo "See docs/GUI_SETUP.md for the full GUI/sandbox setup checklist."
echo

# 4. Directories.
mkdir -p "$WORKSPACE" "$DATA_DIR" "$HOME/.config/hydra"
chmod 700 "$DATA_DIR" || true
echo "Created: $WORKSPACE"
echo "Created: $DATA_DIR"

# 5. Token (0600). Never printed, never overwritten here.
if [ -f "$TOKEN_FILE" ]; then
  chmod 600 "$TOKEN_FILE"
  echo "Token file already exists (kept): $TOKEN_FILE"
else
  python3 "$REPO_DIR/scripts/generate_token.py" --token-file "$TOKEN_FILE"
fi
echo

# 6. Config example (only if user has no config yet).
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
echo "     python3 hydra_client.py search --pattern TODO"
echo "     python3 hydra_client.py screen-shot --save-to shots/shot.png"
echo "   (The last two need the optional tools above; without them"
echo "    you get a clear error naming the missing tool.)"
echo
echo "5) Stop anytime with Ctrl-C in the daemon terminal."
echo "   No auto-start is installed. See README.md for uninstall."
