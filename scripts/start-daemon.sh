#!/usr/bin/env bash
# Start the HYDRA daemon in the foreground (manual start/stop only).
# Usage: ./scripts/start-daemon.sh [--bind 127.0.0.1] [--port 8765] [...]
# All arguments are passed through to `python3 -m hydra`.
# Stop with Ctrl-C. Nothing here enables auto-start.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
exec python3 -m hydra "$@"
