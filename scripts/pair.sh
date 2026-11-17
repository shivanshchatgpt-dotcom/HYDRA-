#!/usr/bin/env bash
# HYDRA pairing card (READ-ONLY diagnostic).
#
# Prints: this laptop's Tailscale IP, the daemon URLs, the token
# file's SHA-256 fingerprint (FIRST 12 HEX CHARS ONLY), the token
# file's permission bits, systemd unit status, and next steps.
#
# This script NEVER prints the token value, NEVER writes anything,
# NEVER changes any service, and NEVER needs elevated privileges.
# It only reads local state and prints a card for Baji to check.
#
# The token itself is handed over for a session only through the
# approved route, separately from this card. Never paste the token
# into chat, screenshots, or this card's output.
#
# Env overrides (same names the daemon uses):
#   HYDRA_PORT, HYDRA_DATA_DIR, HYDRA_TOKEN_FILE, HYDRA_WORKSPACE
set -euo pipefail

PORT="${HYDRA_PORT:-8765}"
DATA_DIR="${HYDRA_DATA_DIR:-$HOME/.local/share/hydra}"
TOKEN_FILE="${HYDRA_TOKEN_FILE:-$DATA_DIR/token}"
WORKSPACE="${HYDRA_WORKSPACE:-$HOME/hydra-workspace}"

echo "=== HYDRA pairing card (read-only) ==="
echo

# 1. Tailscale IP + backend state (local CLI only).
TS_IP=""
TS_STATE="unknown"
TS_HOST="unknown"
if command -v tailscale >/dev/null 2>&1; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  TS_JSON="$(tailscale status --json 2>/dev/null || true)"
  if [ -n "$TS_JSON" ] && command -v python3 >/dev/null 2>&1; then
    TS_LINE="$(printf '%s' "$TS_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); s=d.get("Self") or {}; print((d.get("BackendState") or "unknown"), (s.get("HostName") or "unknown"))' 2>/dev/null || echo "unknown unknown")"
    TS_STATE="${TS_LINE%% *}"
    TS_HOST="${TS_LINE#* }"
  fi
  echo "Tailscale: present (state: $TS_STATE, host: $TS_HOST)"
else
  echo "Tailscale: NOT INSTALLED (daemon is reachable on localhost only)"
fi
if [ -n "$TS_IP" ]; then
  echo "Tailscale IP: $TS_IP"
  echo "Daemon URL (Tailscale): http://$TS_IP:$PORT"
else
  echo "Tailscale IP: none (Tailscale down or not logged in)"
  echo "Daemon URL (Tailscale): unavailable"
fi
echo "Daemon URL (localhost): http://127.0.0.1:$PORT"
echo

# 2. Token fingerprint: SHA-256, first 12 chars ONLY. The token
#    value is never printed, and the file is never copied.
if [ -f "$TOKEN_FILE" ]; then
  if command -v python3 >/dev/null 2>&1; then
    FP="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:12])' "$TOKEN_FILE" 2>/dev/null || echo "unreadable")"
    MODE="$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$TOKEN_FILE" 2>/dev/null || echo "unknown")"
  else
    FP="unknown (python3 missing)"
    MODE="unknown"
  fi
  echo "Token file: $TOKEN_FILE (mode $MODE; must be 0o600)"
  echo "Token fingerprint (SHA-256, first 12 chars): $FP"
  echo "(Fingerprint only — the token value is never shown here.)"
else
  echo "Token file: MISSING ($TOKEN_FILE)"
  echo "Token fingerprint: unavailable"
  echo "Generate one with: python3 scripts/generate_token.py"
fi
echo

# 3. Workspace + systemd unit status (read-only queries only).
if [ -d "$WORKSPACE" ]; then
  echo "Workspace: $WORKSPACE (exists)"
else
  echo "Workspace: $WORKSPACE (MISSING — the daemon creates it on start)"
fi
if command -v systemctl >/dev/null 2>&1; then
  UNIT_ENABLED="$(systemctl --user is-enabled hydra 2>/dev/null || echo "unknown")"
  UNIT_ACTIVE="$(systemctl --user is-active hydra 2>/dev/null || echo "unknown")"
  echo "systemd unit hydra: enabled=$UNIT_ENABLED, active=$UNIT_ACTIVE"
else
  echo "systemd unit hydra: unknown (systemctl not found)"
fi
echo

echo "Next steps:"
echo "  1) If Tailscale is down, bring it up yourself (see"
echo "     docs/TAILSCALE_AUTO.md — the daemon cannot do this)."
echo "  2) Start the daemon with: ./scripts/start-daemon.sh --bind tailscale"
echo "  3) Check readiness with: POST /system/doctor (see docs/TAILSCALE_AUTO.md)"
echo "  4) Hand the token over for this session only, through the"
echo "     approved route. Never paste it into chat or screenshots."
echo "=== end of pairing card ==="
