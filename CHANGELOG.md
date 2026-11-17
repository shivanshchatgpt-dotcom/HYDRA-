# CHANGELOG — HYDRA

All notable changes. Versions follow the daemon's `VERSION` in
`hydra/daemon.py` / `hydra/__init__.py`.

## 0.5.0 — 2026-11-18

Added:

- `POST /web/search` + `POST /web/fetch` (`hydra/web.py`): DuckDuckGo
  HTML search (no key, no cookies) and one capped GET with HTML
  reduced to text. SSRF guard refuses loopback/RFC1918/link-local/
  CGNAT(100.64/10)/NAT64-embedded private targets before any socket
  opens, and re-validates every redirect hop. URL path/query are never
  logged (host + hash + sizes only).
- `hydra/mcp_server.py`: a thin stdio MCP adapter that exposes the
  daemon's audited endpoints as 36 `hydra_*` tools, so any MCP host
  (Muse Code, Claude Code, Codex) can use HYDRA as its body. No new
  privileges: every call is one HTTP+JSON request with the same
  bearer token, workspace jail, blocklist, and audit log. Includes
  `--selfcheck` (schema validation + daemon probe) and `--list-tools`.
  See `docs/MCP_SETUP.md`.
- `hydra_web_search` / `hydra_web_fetch` MCP tools (38 total),
  `tests/test_web.py` (14 tests: URL policy, NAT64/CGNAT handling,
  parsers, redirect refusal, truncation).

## 0.4.0 — 2026-11-17

Added:

- Headless video pipeline (`hydra/video.py`, stdlib only): ffprobe
  media probing (duration/streams/resolution/fps/codec, parsed from
  fixed-argv JSON output), a validated timeline model (multi-track
  video/audio clips with in/out trims, still images, pango text
  titles, crossfade/mix transitions between adjacent tracks,
  per-clip volume converted to dB for MLT's volume filter, SRT/VTT
  subtitle sidecar), MLT XML generation with `xml.etree`
  (profile/producers/playlists/`main` tractor, blanks for gaps),
  `melt` rendering with fixed consumer arguments (libx264 CRF 18 +
  AAC 192k), ffmpeg subtitle burn as a post-pass (a burn failure
  keeps the unburned render and records a warning), output
  validation (video stream + duration within max(0.5s, 2%)),
  ffmpeg PNG preview frames (magic-byte verified), and asset
  manifests (`manifest.json`: SHA256 + probe + source/license note
  per asset; URLs are provenance notes only — the daemon never
  downloads anything and has no auto-scraper). Profiles:
  `1080p30`, `1080p30-vertical`, `720p30`, `1080p25`. No GUI is
  ever opened; Kdenlive itself is only needed to open projects by
  hand.
- Long-horizon video workflows (`hydra/workflows.py`): a fixed
  six-step DAG (ingest -> probe -> timeline -> render -> validate
  -> preview) checkpointed in a separate SQLite database
  (`video.db`, same WAL + row pattern as `jobs.db`). Workflows
  interrupted by a daemon restart are marked `interrupted` (any
  still-living recorded melt process is killed) and resume
  explicitly via `/video/render/poll` with `{"resume": true}` —
  completed steps are never re-run, and nothing ever auto-resumes
  (no scheduler, no agent loop). Transient render failures retry
  up to 3 times with backoff (2s, 8s); permanent failures (bad
  timeline, missing tool, disk guard) fail immediately. One melt
  render at a time (single-render lock; later workflows wait in
  `queued`). Render timeout default 4h, max 24h.
- Disk guard: renders are refused (HTTP 507) when free space is
  below a conservative estimate (planning bitrate x duration x 2.0
  safety factor + 100 MB headroom), checked at start and again
  right before melt runs.
- New endpoints, all auth + audit + jail checked like existing
  routes: `POST /video/probe`, `/video/project/create`,
  `/video/render/start` (with `dry_run`, which validates + estimates
  without creating a workflow; melt missing returns 501 before any
  workflow is created), `/video/render/poll` (progress percent +
  current step + per-step attempts/errors; `resume` flag),
  `/video/preview` (workspace file paths only, never image bytes),
  `/video/list` (projects + workflows + `render_busy`). `GET
  /health` gains `video_tools` presence booleans (kdenlive/melt/
  ffmpeg/ffprobe, no version guessing). `/shutdown` now also
  interrupts running video workflows (response adds
  `workflows_interrupted`; interrupted workflows stay resumable).
- `docs/VIDEO_SETUP.md`: Arch install (`sudo pacman -S kdenlive
  mlt ffmpeg`), the six steps, resume rules, disk-space guidance,
  and the live laptop verification checklist.
- 80 new video tests: ffprobe parsing with fake ffprobe
  JSON/binary, timeline validation, MLT XML generation parsed
  back with ElementTree, manifest/disk-guard units, workflow
  checkpoint/resume/retry/queue-exclusivity tests, and HTTP
  end-to-end tests including 401s, path-escape rejection, honest
  501s without melt, and a full render against a fake melt (real
  ffmpeg/ffprobe) proving the HTTP -> workflow -> render ->
  validate -> preview chain.
- Tailscale-auto, folded into the same 0.4.0 release (no version
  bump): bind mode `tailscale` (`hydra/tailscale.py`, stdlib
  only) resolving the current Tailscale IPv4 via local
  `tailscale ip -4` (fixed argv, scrubbed env, CGNAT-validated)
  and failing closed with a clear error when Tailscale is down,
  missing, or returns no address; default bind unchanged
  (`127.0.0.1`) and the same loopback + 100.64.0.0/10 validation
  applies after resolution. Read-only endpoints, all auth + audit
  like `/system/info`: `POST /system/tailscale` (connected,
  backend state, self hostname/IP, version, peer count only — no
  node keys, no peer list; 501 when the binary is missing, 502
  when tailscaled is down; a parseable NeedsLogin state is a
  normal 200 with `connected: false`) and `POST /system/doctor`
  (`hydra/doctor.py`: tailscale/tools/disk-floor/token-mode/
  workspace/systemd-unit/bind checks with fix hints, never
  auto-fix, token contents never read). `GET /health` gains
  `tailscale` presence/connection/IP fields that degrade honestly
  and never fail the health check. Laptop scripts: `scripts/pair.sh`
  (read-only pairing card, token SHA-256 fingerprint first 12
  chars only, never the token value), `scripts/setup-tailscale.sh`
  (checks + printed commands only, never executes sudo; disk
  warning refusing to suggest a full `pacman -Syu` when space is
  low; opt-in `--enable-autostart` runs user-scope
  `systemctl --user` commands only when the unit file is already
  installed), and `scripts/kill-switch.sh` (local only, no daemon
  endpoint; deletes token + data only with explicit `--confirm`
  after printing exactly what will be deleted, with guards against
  unsafe data-dir values). `docs/TAILSCALE_AUTO.md` documents the
  setup flow, pairing card, doctor output, kill switch, and the
  honest limits: the per-session token handoff is NOT removed, no
  relay/Cloudflare/polling/HTTPS/public bind/credential storage/
  auto-update is added, the laptop must still be on and awake,
  and MagicDNS/Tailscale Serve are listed only as unverified
  future investigation.
- 47 new tests (268 total, 1 skipped for missing `bwrap`):
  Tailscale bind resolution + fail-closed with a fake `tailscale`
  binary on PATH (up/down/bad-IP/empty/missing), status-JSON
  parsing with keys/peers dropped, doctor checks + disk-floor
  thresholds + the no-secret guarantee (token value, node keys,
  and peer identities asserted absent from every response, report,
  script output, and audit entry), HTTP end-to-end tests for both
  new endpoints including 401s/501/502 and the `/health` fields,
  and script tests (pair fingerprint prefix only, setup script
  sudo-only-in-printed-text + autostart flag behaviour + low-disk
  warning, kill-switch confirm/guard behaviour).

Changed:

- `config.example.json`: version comment bumped; no new keys (video
  settings are per-call, like browser control). The comment now
  also notes the `tailscale` bind value.
- Error shape gains HTTP 507 for disk-guard refusals; HTTP 409 now
  also covers existing projects and unresumable workflows. HTTP
  501 now also covers a missing `tailscale` binary (on
  `/system/tailscale`) and HTTP 502 also covers a down tailscaled
  or unparseable status output.

Safety invariants kept from v0.1/v0.2/v0.3 (unchanged):

- Bind rules, 0600 token file + constant-time compare, workspace
  jail with symlink resolution, blocklist failing closed, audit log
  with hashes/lengths only, stdlib-only, zero outbound calls from
  the daemon, no model, no telemetry, no agent loop, no scheduling
  inside the daemon, no credential handling of any kind. Video
  subprocesses (ffprobe/melt/ffmpeg) run with fixed argument lists
  (no shell, no caller-supplied flags).

Deliberately NOT added: GUI automation for video, `.kdenlive`
project generation, keyframed effects/colour grading, media
downloading or stock search, automatic resume, parallel renders,
CPU/RAM limits on melt, `hydra_client.py` subcommands for `/video/*`.

Not verified in this release (needs Baji's laptop — see
`docs/VIDEO_SETUP.md` and `docs/TAILSCALE_AUTO.md`): real melt renders of HYDRA-generated MLT,
crossfade rendering (luma transition carries no resource file),
pango title rendering, volume audibility, subtitle burn output,
Kdenlive round-trip (opening `project.mlt` by hand), interruption
+ resume across a real daemon restart on the laptop, and the
disk-guard refusal on a nearly-full disk. The suite proves the
plumbing against fake melt + real ffmpeg/ffprobe only; no
Kdenlive/melt exists on the build VM. Also unverified:
Tailscale-auto against a real tailscaled and systemd user
session (no tailscaled/systemd user session on the build VM) —
`--bind tailscale` startup, `/system/tailscale` and
`/system/doctor` against real state, the pairing card, the
setup/kill-switch scripts, and the unit with `--bind tailscale`
after a reboot; see the checklist in `docs/TAILSCALE_AUTO.md`.
The Tailscale-auto suite proves the logic against fake
`tailscale`/`systemctl` binaries only.

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
