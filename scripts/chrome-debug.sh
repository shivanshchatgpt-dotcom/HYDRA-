#!/usr/bin/env bash
# HYDRA v0.3 — launch Chrome with remote debugging for CDP browser control.
#
# What this script DOES:
#   * checks whether Chrome is ALREADY listening on the debug port
#     (127.0.0.1:9222) and exits 0 if so — no second instance is started
#   * otherwise launches Chrome ONCE with --remote-debugging-port=9222
#     using Baji's NORMAL profile (so logged-in sessions stay usable)
#
# What this script NEVER does:
#   * NEVER kills, restarts, or touches a running Chrome instance
#   * NEVER uses a throwaway/temp profile (a fresh profile would be
#     logged out of everything and useless for Baji's own work)
#   * NEVER runs sudo, never touches the network beyond localhost
#
# IMPORTANT: if Chrome is already running WITHOUT the debug flag,
# launching a second copy with the flag will NOT enable debugging —
# the new process just hands off to the running instance. In that
# case: close Chrome completely first (all windows), then run this
# script. See docs/BROWSER_SETUP.md.
#
# Usage:
#   ./scripts/chrome-debug.sh            # default port 9222
#   ./scripts/chrome-debug.sh 9333       # custom port (tell the daemon:
#                                        # /browser/* calls take "port")
#   ./scripts/chrome-debug.sh 9222 --kiosk   # extra Chrome flags pass through
set -euo pipefail

PORT="${1:-9222}"
shift || true
# Remaining args (if any) are passed through to Chrome unchanged.

# Already listening? Then debugging is already on — do nothing.
if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Chrome remote debugging is already up on 127.0.0.1:${PORT}."
  echo "Nothing to launch. Verify with: python3 hydra_client.py browser-status --port ${PORT}"
  exit 0
fi

CHROME=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROME="$candidate"
    break
  fi
done
if [ -z "$CHROME" ]; then
  echo "ERROR: no Chrome/Chromium binary found (looked for google-chrome,"
  echo "google-chrome-stable, chromium, chromium-browser)."
  echo "Install Chrome yourself, then run this script again."
  exit 1
fi

echo "Launching: $CHROME --remote-debugging-port=${PORT} (normal profile)"
echo "Close Chrome normally when you are done; this script never kills it."
echo "Verify with: python3 hydra_client.py browser-status --port ${PORT}"
exec "$CHROME" --remote-debugging-port="$PORT" "$@"
