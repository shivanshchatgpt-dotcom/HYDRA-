# CHANGELOG — HYDRA

All notable changes. Versions follow the daemon's `VERSION` in
`hydra/daemon.py` / `hydra/__init__.py`.

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
