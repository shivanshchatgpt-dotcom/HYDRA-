#!/usr/bin/env bash
# HYDRA v0.4 Arch Linux setup (Baji's laptop: Arch + KDE Plasma + Wayland).
#
# What this script DOES:
#   * checks python3 (>= 3.11) and tailscale presence (read-only checks)
#   * checks the OPTIONAL tools (ripgrep, bubblewrap, ydotool,
#     spectacle/grim, Chrome/Chromium for CDP browser control,
#     kdenlive/mlt/ffmpeg for headless video) and
#     prints install guidance for the missing ones
#   * creates ~/hydra-workspace and ~/.local/share/hydra
#   * generates a 0600 bearer token file if one does not exist
#   * prints exact next steps (Tailscale login, start, test), including
#     the OPT-IN autostart commands (printed only, never run)
#
# What this script NEVER does:
#   * never runs sudo / pacman -S (install guidance is printed only;
#     Baji runs it himself after reading it)
#   * never changes firewall rules
#   * never enables auto-start (the systemd unit has Restart=always,
#     but installing/enabling it is a separate, manual, opt-in step;
#     this script only PRINTS the commands — see README.md)
#   * never touches the network, never phones home
#
# v0.4 scope: files + shell + patch edits + search + PERSISTENT and
# DETACHED background jobs inside the workspace, optional bubblewrap
# sandbox, best-effort GUI endpoints (screenshot + input), read-only
# system/git endpoints, loopback-only CDP browser control when
# Chrome is running with --remote-debugging-port (see docs/BROWSER_SETUP.md),
# and headless video (MLT XML + melt, no GUI; see docs/VIDEO_SETUP.md).
# NO agent loop, NO scheduling inside the daemon, NO model — Mikey in
# the cloud is the brain; this daemon is only the body.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${HYDRA_WORKSPACE:-$HOME/hydra-workspace}"
DATA_DIR="${HYDRA_DATA_DIR:-$HOME/.local/share/hydra}"
TOKEN_FILE="${HYDRA_TOKEN_FILE:-$DATA_DIR/token}"

echo "=== HYDRA v0.4 setup (Arch Linux) ==="
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

# 3. Optional v0.4 tools (read-only checks; install guidance only).
# The daemon works without all of these; each missing tool only
# disables the endpoints that need it, with a clear 501 error.
echo "Optional v0.4 tools:"
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
if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1; then
  echo "  Chrome/Chromium: found — /browser/* endpoints available once Chrome"
  echo "    is running with remote debugging (see scripts/chrome-debug.sh)."
else
  echo "  Chrome/Chromium: NOT FOUND — /browser/* endpoints return 501."
  echo "    Install Chrome yourself (AUR: google-chrome) if you want them."
fi
if command -v melt >/dev/null 2>&1; then
  echo "  melt: found — /video/render/* endpoints available"
else
  echo "  melt: NOT FOUND — /video/render/* endpoints return 501."
  echo "    To install it yourself: sudo pacman -S mlt"
fi
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  echo "  ffmpeg/ffprobe: found — /video/probe and /video/preview available"
else
  echo "  ffmpeg/ffprobe: NOT FOUND — /video/probe and /video/preview return 501."
  echo "    To install them yourself: sudo pacman -S ffmpeg"
fi
if command -v kdenlive >/dev/null 2>&1; then
  echo "  kdenlive: found — you can open generated .mlt projects by hand"
else
  echo "  kdenlive: NOT FOUND — headless renders still work without it;"
  echo "    install it yourself (sudo pacman -S kdenlive) only if you want"
  echo "    to open projects in the Kdenlive GUI."
fi
echo "See docs/GUI_SETUP.md for the GUI/sandbox checklist,"
echo "docs/BROWSER_SETUP.md for the Chrome debugging checklist, and"
echo "docs/VIDEO_SETUP.md for the headless video checklist."
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
echo "     ./scripts/start-daemon.sh --bind tailscale"
echo "   (The daemon resolves the laptop's current Tailscale IP itself"
echo "    and refuses to start if Tailscale is down. The manual form,"
echo "    ./scripts/start-daemon.sh --bind <TAILSCALE-IP>, still works;"
echo "    see docs/TAILSCALE_AUTO.md. ./scripts/pair.sh prints a"
echo "    read-only pairing card with the daemon URL.)"
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
echo "   No auto-start is installed by this script. See README.md."
echo
echo "6) OPTIONAL autostart (opt-in only — these commands are PRINTED,"
echo "   NOT run by this script; run them yourself only if you want the"
echo "   daemon to come back on its own after a crash or a reboot):"
echo "     mkdir -p ~/.config/systemd/user"
echo "     cp systemd/hydra.service ~/.config/systemd/user/hydra.service"
echo "     systemctl --user daemon-reload"
echo "     systemctl --user enable --now hydra"
echo "   The unit has Restart=always, so a crashed daemon restarts in"
echo "   5 seconds. An explicit 'systemctl --user stop hydra' still"
echo "   stops it for good. Detached jobs survive daemon restarts;"
echo "   see docs/AUTONOMY.md for what runs without you present."
echo
echo "7) OPTIONAL browser control: start Chrome with remote debugging:"
echo "     ./scripts/chrome-debug.sh"
echo "   (Uses your normal profile; never kills a running Chrome.)"
echo "   Then: python3 hydra_client.py browser-status"
echo
echo "8) Tailscale setup helpers (v0.4, see docs/TAILSCALE_AUTO.md):"
echo "     ./scripts/setup-tailscale.sh   # checks + prints setup commands"
echo "     ./scripts/pair.sh              # read-only pairing card"
echo "   And once the daemon is running, Mikey can ask it for a"
echo "   self-check (POST /system/doctor) and Tailscale state"
echo "   (POST /system/tailscale) — both read-only, both need the token."
