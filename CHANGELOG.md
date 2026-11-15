# CHANGELOG — HYDRA

All notable changes. Versions follow the daemon's `VERSION` in
`hydra/daemon.py` / `hydra/__init__.py`.

## 0.3.0 — 2026-11-16

Added:

- Persistent background jobs: job records now live in a separate
  SQLite database (`jobs.db` in the data dir) with per-job log files
  (`job-logs/<job_id>.out|.err`). Records and output survive daemon
  restarts; still-running jobs are re-attached by PID liveness
  (`os.kill(pid, 0)`), guarded against PID reuse with the process
  start time from `/proc/<pid>/stat`. Re-attached jobs report
  `recovered: true`; a re-attached job found dead reports `done`
  with `returncode: null` (unknown — never faked). Deadlines are
  wall-clock so the reaper enforces them after a restart. The 32-job
  cap and whole-process-group kills are unchanged; pruning now also
  deletes the database row and log files.
- Detached jobs: `"detached": true` on `/jobs/start` runs the job in
  its own session with output straight to its log files, and the job
  is NOT killed on daemon shutdown — it survives crashes and
  restarts and is re-attached on the next start. `/jobs/kill` kills
  a detached job's whole process group. `/jobs/*` request/response
  shapes stay backward compatible (additive `detached`, `pid`,
  `recovered` fields only).
- Job log safety: persistent job output is capped at read time
  (200KB/stream, same as `/exec`) and log files are hard-capped at
  1MB per stream via `RLIMIT_FSIZE`, so a runaway job cannot fill
  the disk (it is killed by the kernel with SIGXFSZ at the cap).
- Resilience: the systemd user unit now has `Restart=always` +
  `RestartSec=5` (a crashed daemon comes back in 5 seconds; an
  explicit `systemctl --user stop` still stops it for good). The
  install script prints the opt-in autostart commands and NEVER runs
  them. The daemon writes a PID file (`hydra.pid` in the data dir,
  removed on clean exit). `GET /health` gains `uptime_seconds` plus
  `jobs_running` / `jobs_finished` counts.
- Read-only endpoints: `POST /system/info` (disk free on the
  workspace filesystem, load average, memory from `/proc/meminfo`,
  uptime, tool presence for rg/bwrap/ffmpeg/ydotool/spectacle/grim)
  and `POST /git/status` (branch, ahead/behind vs upstream when
  configured, short porcelain status, capped at 200 entries;
  workspace-only, `GIT_OPTIONAL_LOCKS=0`, no commit/push behaviour
  anywhere in these endpoints).
- CDP browser control, loopback only: a hand-rolled stdlib WebSocket
  client (`hydra/websocket.py`, RFC 6455: masking, fragmentation,
  ping/pong, close, 16/64-bit lengths — no new dependencies) plus
  `hydra/browser.py` and `POST /browser/status|navigate|evaluate|
  content|shot|click|type`. Any non-loopback CDP target is refused
  (403) before a socket is opened; navigation allows http/https and
  `about:blank` only (`file:`/`data:`/`chrome:` refused, so the
  browser cannot bypass the workspace jail); evaluate/content
  results are capped with `truncated` flags; audit entries carry
  lengths/hashes only, never page content or typed text. Missing
  Chrome returns 501 naming `scripts/chrome-debug.sh`; CDP protocol
  failures return 502. `scripts/chrome-debug.sh` launches Chrome
  with `--remote-debugging-port=9222` on the normal profile and
  never kills an existing Chrome.
- `docs/BROWSER_SETUP.md` (exact Chrome launch, troubleshooting,
  live CDP checklist) and `docs/AUTONOMY.md` (what runs without
  Baji present, what still needs him, v0.3 vs OpenCode vs Hermes
  Agent comparison).
- `hydra_client.py` subcommands for every new endpoint, plus
  `--detached` on `jobs-start`.
- 66 new tests (141 total, 1 skipped for missing `bwrap`): WebSocket
  unit tests, CDP tests against an independent fake CDP server
  (`tests/fake_cdp.py`, stdlib sockets, handshake + frames), job
  persistence/restart tests, HTTP end-to-end tests for all new
  endpoints including 401s and non-loopback refusal.

Changed:

- `scripts/install-arch.sh` (v0.3): also checks for Chrome/Chromium,
  prints the opt-in autostart commands (never runs them), and the
  browser-control setup step. Still never runs sudo/pacman.
- `systemd/hydra.service` (v0.3): `Restart=always`, `RestartSec=5`,
  plus notes on detached jobs and the CDP prerequisite.
- `config.example.json`: version comment bumped; no new keys.
- Error shape gains HTTP 502 for CDP protocol failures (Chrome
  answered but the command failed); see `docs/ARCHITECTURE.md`.

Safety invariants kept from v0.1/v0.2 (unchanged):

- Bind rules (localhost/Tailscale only; `0.0.0.0` refused without
  `--allow-public`), 0600 token file + constant-time compare,
  workspace jail with symlink resolution, blocklist failing closed,
  audit log with hashes/lengths only (now also for browser calls),
  stdlib-only, zero outbound calls from the daemon, no model, no
  telemetry, no agent loop, no scheduling inside the daemon, no
  credential handling of any kind.

Deliberately NOT added (see `docs/AUTONOMY.md`): agent loop,
cron/scheduling inside the daemon, model or embedding code, HTTPS,
per-request approval queue, credential extraction.

Not verified in this release (needs Baji's laptop — see
`docs/BROWSER_SETUP.md` and `docs/AUTONOMY.md`): CDP against real
Chrome (the suite proves the protocol against a fake server only),
the autostart unit's restart behaviour on the laptop, and detached
jobs across a real daemon restart on the laptop. GUI success paths
and real bubblewrap execution were verified live for v0.2 except
screenshots (unreliable spectacle) and input (ydotoold not running);
see `docs/GUI_SETUP.md`.

## 0.2.0 — 2026-11-15

Added:

- `POST /edit`: patch-style exact `old_string` -> `new_string`
  replacement. Fails with 400 on zero matches and 409 on ambiguous
  matches (unless `replace_all: true`); `dry_run` previews without
  writing; strict UTF-8, newline-preserving.
- `POST /read` line ranges: optional 1-based inclusive `start_line` /
  `end_line`; response adds `start_line`, `end_line`, `lines_returned`,
  `total_lines`. No-range behaviour is unchanged from v0.1.
- `POST /search`: workspace code search, ripgrep (`rg --json`) when
  installed, grep fallback otherwise. Glob filter, fixed-strings and
  case-sensitivity options, result caps. Audit log records pattern
  length/hash only, never the pattern.
- Background jobs: `POST /jobs/start|poll|kill|list`. Same jail +
  blocklist + sandbox validation as `/exec`, enforced at start. Output
  polling by byte offset, whole-process-group kill, deadline reaper,
  32-job cap (429 when full), jobs killed on shutdown/exit.
- Opt-in bubblewrap sandbox for `/exec` and `/jobs/start`
  (`"sandbox": true`, `"sandbox_no_network"` default true): system
  read-only, workspace read-write, fresh `/tmp`, own namespaces.
  Missing `bwrap` returns 501 naming bubblewrap — never silently
  unsandboxed.
- `POST /memory/delete` (memory by `id`, relation by `relation_id`)
  and `POST /memory/list` (newest first, optional `kind` filter).
- Best-effort GUI endpoints (Wayland/KDE): `POST /screen/shot`
  (spectacle -> grim -> xdg-desktop-portal, base64 PNG or workspace
  file, PNG verified) and `POST /input/move|click|type` (ydotool).
  Missing tools/session return 501 naming the gap; success is never
  faked. Typed text is never audit-logged.
- `hydra_client.py` subcommands for every new endpoint.
- `docs/GUI_SETUP.md`: exact laptop setup, permissions, and the live
  verification checklist for GUI + sandbox.
- References section in `docs/ARCHITECTURE.md` (OpenCode, Hermes
  Agent) and browser/CDP control documented as future work.

Changed:

- Audit entries are now written **before** the response is sent
  (previously after), so a returned result is always already logged.
  This also fixes a race where a fast client could read the audit
  file before the entry landed.
- `/exec` response adds `"sandboxed": bool`.
- `/shutdown` response adds `"jobs_killed"` (running jobs are killed
  before the server stops).
- `scripts/install-arch.sh` now checks the optional v0.2 tools
  (ripgrep, bubblewrap, ydotool, spectacle/grim) and prints install
  guidance only — still never runs sudo/pacman.
- `systemd/hydra.service` notes that GUI endpoints need the daemon
  started inside the graphical session.

Safety invariants kept from v0.1 (unchanged):

- Bind rules (localhost/Tailscale only; `0.0.0.0` refused without
  `--allow-public`), 0600 token file + constant-time compare,
  workspace jail with symlink resolution, blocklist failing closed,
  audit log with hashes/lengths only, stdlib-only, zero outbound
  calls, no model, no telemetry, no fallback agent loop.

Not verified in this release (needs Baji's laptop — see
`docs/GUI_SETUP.md`): real screenshot capture on KDE/Wayland,
ydotool input end-to-end, bubblewrap sandboxing on the Arch kernel.
The automated suite covers their failure paths (501s) and the
sandbox argv structure, not live execution.

## 0.1.0 — 2026-11-15

Initial harness release:

- Stdlib-only Python daemon: `GET /health`, `POST /exec|read|write|
  list|shutdown`, `POST /memory/add|search|graph/add|graph/neighbors`.
- Bearer-token auth (0600 file, constant-time compare), workspace
  jail, command blocklist (fails closed), audit log, SQLite FTS5 +
  graph memory (no model, no vectors), Tailscale/localhost-only bind.
- `hydra_client.py`, `scripts/install-arch.sh`,
  `scripts/start-daemon.sh`, `scripts/generate_token.py`, opt-in
  systemd user unit, `config.example.json`.
- 30-test stdlib unittest suite (`tests/test_daemon.py`).
- Repurposed from the HYDRA clipping-system plan; the original README
  is preserved verbatim at `docs/CLIPPING_PLAN_ARCHIVED.md`.
