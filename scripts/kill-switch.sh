#!/usr/bin/env bash
# HYDRA kill switch (LOCAL SCRIPT ONLY).
#
# Stops Mikey's access: stops the HYDRA systemd user unit (if
# installed), prints the network cut-off command for Baji to run
# himself, and — ONLY with the explicit --confirm flag — deletes
# the token + daemon data directory after printing exactly what
# will be deleted.
#
# This script is NEVER triggered remotely: there is no daemon
# endpoint that runs it. Baji runs it in his own terminal.
#
# Without --confirm, nothing is deleted: the script prints what
# WOULD be deleted and exits, leaving all files in place.
#
# Detached jobs keep running after the daemon stops (by design).
# Kill them explicitly if that matters; this script only prints
# how, it never kills user processes itself.
#
# Env overrides (same names the daemon uses):
#   HYDRA_DATA_DIR, HYDRA_TOKEN_FILE
set -euo pipefail

CONFIRM=0
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=1 ;;
    --help|-h)
      echo "Usage: scripts/kill-switch.sh [--confirm]"
      echo "Stops the hydra user unit and prints revocation steps."
      echo "With --confirm, also deletes the token + data directory"
      echo "after printing exactly what will be deleted."
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: scripts/kill-switch.sh [--confirm]" >&2
      exit 2
      ;;
  esac
done

DATA_DIR="${HYDRA_DATA_DIR:-$HOME/.local/share/hydra}"
TOKEN_FILE="${HYDRA_TOKEN_FILE:-$DATA_DIR/token}"

echo "=== HYDRA kill switch ==="
echo

# 1. Stop the daemon's user unit (user scope only). A manually
#    started daemon (foreground terminal) is NOT stopped here:
#    stop it with Ctrl-C or POST /shutdown in that terminal.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user stop hydra 2>/dev/null; then
    echo "Stopped: hydra user unit."
  else
    echo "hydra user unit: not running or not installed (nothing stopped)."
  fi
else
  echo "systemctl not found: no unit to stop."
fi
echo "If you started the daemon by hand, stop it too (Ctrl-C in its"
echo "terminal, or POST /shutdown). After that, Mikey has no access."
echo

# 2. Network cut-off: PRINTED ONLY, never executed by this script.
echo "To cut the private network too, run this yourself:"
echo "  sudo tailscale down"
echo "(Or remove the Muse device in the Tailscale admin console.)"
echo

# 3. Token + data deletion: guarded, explicit, printed first.
RESOLVED="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$DATA_DIR")"
HOME_RESOLVED="$(python3 -c 'import os; print(os.path.realpath(os.path.expanduser("~")))')"
if [ -z "$DATA_DIR" ] || [ "$RESOLVED" = "/" ] || [ "$RESOLVED" = "$HOME_RESOLVED" ] || [ "$RESOLVED" = "$HOME_RESOLVED/.local/share" ]; then
  echo "REFUSING to delete: resolved data dir is unsafe: $RESOLVED" >&2
  exit 2
fi
echo "Token + data directory: $RESOLVED"
if [ -d "$RESOLVED" ]; then
  echo "Contents that would be deleted:"
  find "$RESOLVED" -maxdepth 2 -print | sort | sed 's/^/  /'
  echo "This includes the token file ($TOKEN_FILE), jobs.db,"
  echo "video.db, job logs, memory, and the audit log."
else
  echo "Directory does not exist: nothing to delete."
  echo "=== kill switch done (access stopped; nothing deleted) ==="
  exit 0
fi
echo
if [ "$CONFIRM" -ne 1 ]; then
  echo "NOT deleted (no --confirm flag). Files are untouched."
  echo "To delete the token + data above, re-run with --confirm:"
  echo "  ./scripts/kill-switch.sh --confirm"
  echo "=== kill switch done (access stopped; files kept) ==="
  exit 0
fi
echo "Deleting with --confirm: $RESOLVED"
rm -rf -- "$RESOLVED"
echo "Deleted: $RESOLVED"
echo
echo "Detached jobs (if any) are STILL running: they survive daemon"
echo "stop by design. Find them with ps and kill them if needed."
echo "To revoke fully, also remove the Muse device in the Tailscale"
echo "admin console. See SECURITY.md (Revocation)."
echo "=== kill switch done ==="
