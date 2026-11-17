# ARCHITECTURE.md — HYDRA v0.4

## Design in one paragraph

Mikey (cloud) thinks. The laptop daemon executes. They talk over
HTTP+JSON on the private Tailscale network, authenticated with a
bearer token. The daemon holds no model, makes no outbound calls,
and keeps only five kinds of local state: the workspace files, an
append-only audit log, a small SQLite memory database, a job
database + log files, and a video workflow database. Everything
else is request/response and forgotten. v0.2 added patch edits,
code search, background jobs, an opt-in bubblewrap sandbox, and
best-effort GUI endpoints (screenshot + input). v0.3 added
persistent + detached jobs, resilience (PID file, restart-friendly
unit, richer `/health`), read-only system/git endpoints, and
loopback-only CDP browser control. v0.4 adds a headless video
pipeline (MLT XML timelines rendered by `melt`, no GUI) with
persistent, explicitly resumable six-step render workflows, plus
Tailscale-auto: a `--bind tailscale` startup mode that resolves
the current Tailscale IP from the local CLI and fails closed when
Tailscale is down, read-only `/system/tailscale` and
`/system/doctor` endpoints, and local pairing/setup/kill-switch
scripts — all still dumb execution, no agent loop on the laptop,
and no change to the security model (see `SECURITY.md` rule 12
and `docs/TAILSCALE_AUTO.md`).

## Components

```text
hydra/daemon.py        ThreadingHTTPServer, routes, /exec runner, jobs wiring
hydra/security.py      jail + blocklist + token checks (audit this first)
hydra/memory_store.py  SQLite: memories + FTS5 + entities + relations
hydra/audit.py         JSONL audit log writer
hydra/search.py        ripgrep (preferred) / grep workspace search
hydra/jobs.py          persistent + detached jobs: jobs.db, log files,
                       re-attach, deadlines, output/log caps
hydra/sandbox.py       bubblewrap argv builder (fails closed when missing)
hydra/gui.py           screenshots + ydotool input (clear errors, no fakes)
hydra/websocket.py     stdlib-only WebSocket client (RFC 6455, for CDP)
hydra/browser.py       CDP browser control, loopback only
hydra/sysinfo.py       read-only system info + git status
hydra/video.py         ffprobe probing, timeline model, MLT XML
                       generation, melt render, validation, previews,
                       asset manifests, disk guard
hydra/workflows.py     persistent resumable 6-step video workflows:
                       video.db, retry/backoff, single-render queue
hydra/tailscale.py     local `tailscale` CLI parsing: IP resolution
                       (CGNAT-validated, fail-closed), status JSON
                       summary with keys/peers dropped
hydra/doctor.py        read-only self-checks: Tailscale, tools, disk
hydra/mcp_server.py    stdio MCP adapter: 38 hydra_* tools -> same daemon
                       routes (thin client, no new privileges)
hydra/web.py           outbound web search (DDG HTML) + fetch, with
                       SSRF guard (public IPs only, redirects rechecked)
                       floor, token file mode (stat only), workspace,
                       systemd unit, bind mode; fix hints, no fixes
hydra_client.py        stdlib client (testing / manual use)
```

Data flow for one request:

1. Mikey sends `POST /exec` with `Authorization: Bearer <token>`.
2. Handler checks the token (constant-time compare). Fail -> 401 + audit.
3. Handler checks the jail (cwd inside workspace) and the blocklist.
   Fail -> 403 + audit. Nothing runs. If `"sandbox": true`, the
   bubblewrap argv is built here; missing `bwrap` -> 501 + audit,
   never a silent unsandboxed run.
4. Command runs via `/bin/sh -c` (or bwrap-wrapped) with cwd forced
   inside the workspace, stdin closed, scrubbed env, timeout, output
   truncation.
5. Audit entry appended (truncated command preview, lengths, exit
   code), *then* the JSON result returns. No token, no secrets in
   the log.

Background jobs (`/jobs/start`) follow steps 1-3, then hand the
process to `JobManager`. In persistent mode (the daemon always runs
this way: `data_dir` is set) the job is recorded in `jobs.db` and
its stdout/stderr go straight to per-job log files under
`job-logs/`; a waiter thread learns the exit code, and a reaper
enforces the wall-clock deadline by killing the whole process
group. On a later daemon start, `JobManager` reloads the table and
re-attaches to still-running jobs by PID liveness, guarded against
PID reuse with the process start time from `/proc/<pid>/stat`.
Detached jobs (`"detached": true`) are skipped by the shutdown
kill-all and keep running across restarts. Non-detached jobs are
still killed on clean shutdown, but their records and output stay
queryable. `/jobs/poll` reads the log files (200KB read cap per
stream, `truncated` flag) and returns incremental stdout by byte
offset. Log files are hard-capped at 1MB per stream via
`RLIMIT_FSIZE`. Without a `data_dir` (direct unit-test construction
only) jobs behave as in v0.2: in-memory, pipes + reader threads,
forgotten on shutdown.

Browser flow: `/browser/*` handlers validate the CDP target first
(loopback only, 403 otherwise), then `hydra/browser.py` opens a
short-lived `CdpSession`: it fetches `/json/list` from Chrome's
debugging port, picks a page target, connects the hand-rolled
WebSocket client to that target's `webSocketDebuggerUrl` (re-validated
as loopback), sends one CDP command, and skips event notifications
until the matching reply arrives. One session per request; no
persistent browser connection is held. Navigation is allowlisted to
http/https + `about:blank`. Audit entries carry lengths/hashes only.

System/git flow: `/system/info` reads `shutil.disk_usage`,
`os.getloadavg`, `/proc/meminfo`, `/proc/uptime`, and
`shutil.which` for six tools — pure reads, no subprocess. `/git/status`
runs fixed git argument lists (`rev-parse`, `rev-list`, `status
--porcelain=v1`) with `GIT_OPTIONAL_LOCKS=0`, a 10s timeout, and a
scrubbed environment; the path is jailed like `/read`.

Tailscale/doctor flow: at startup, a bind value of `tailscale`
(case-insensitive) is resolved by `hydra/tailscale.py` running
`tailscale ip -4` (fixed argv, scrubbed environment, 10s timeout);
the first output line must parse as an IPv4 address inside
`100.64.0.0/10`, or startup fails with a clear error and a
non-zero exit code — the literal word `tailscale` is never passed
to the socket, and the resolved IP still passes the normal bind
validation. `/system/tailscale` runs `tailscale status --json`
the same way and keeps only six fields (connected, backend_state,
self_hostname, self_ip, version, peer_count); node keys, peer
records, and DNS names are dropped during parsing, and the audit
entry carries connected + peer_count only. A parseable
non-Running state (e.g. `NeedsLogin`) is a normal 200 answer with
`connected: false`. `/system/doctor` runs `hydra/doctor.py`:
presence checks via `shutil.which`, Tailscale status via the same
parser, disk free via `shutil.disk_usage` (fail below 1 GiB, warn
below 5 GiB), token file mode via `os.stat` only (contents never
read), workspace existence, read-only `systemctl --user
is-enabled/is-active`, and bind-mode validation; each check
returns pass/warn/fail plus a fix hint naming a command Baji runs
by hand. Nothing in this flow writes, fixes, or calls out.

Memory flow is the same shape, except the store is SQLite and the
search is FTS5 keyword + graph neighbours (no vectors, no model).

GUI flow: `/screen/shot` tries spectacle, then grim, then the
xdg-desktop-portal Screenshot call over D-Bus; the first success
wins and the PNG is verified (magic bytes + IHDR size) before it is
returned. `/input/*` shells out to `ydotool` only. Any failure is a
501 naming the missing tool/permission — success is never faked.

Video flow: `/video/project/create` validates a timeline spec
(tracks/clips/transitions/subtitle/assets), probes the assets with
ffprobe, and writes `video/<name>/timeline.json`,
`manifest.json` (SHA256 + probe + source/license note per asset —
provenance only, nothing is ever downloaded), and `project.mlt`
(MLT XML built with `xml.etree`: profile, producers, one playlist
per track with blanks for gaps, a `main` tractor with multitrack +
transitions + per-entry volume filters). `/video/render/start`
checks melt presence (501 when missing) and the disk guard (507
when free space is below the estimate), then creates a workflow in
`video.db` and starts its runner thread. The runner executes
ingest -> probe -> timeline -> render -> validate -> preview,
checkpointing each step; transient render failures retry up to 3
times with backoff (2s, 8s); the render step holds the
single-render lock while `melt` runs with fixed consumer arguments
(libx264 CRF 18 + AAC), then burns subtitles with ffmpeg when the
timeline has a sidecar (a burn failure keeps the unburned render
and records a warning). `/video/render/poll` returns progress,
current step, and per-step attempts/errors; `{"resume": true}`
resumes an interrupted/failed workflow from its last completed
step. On daemon start, workflows left running/queued are marked
interrupted and any recorded melt process is killed; nothing ever
auto-resumes. `/video/preview` extracts PNG frames with ffmpeg and
returns workspace file paths, never image bytes.

## Endpoints

Base URL (examples): `http://127.0.0.1:8765` locally, or
`http://<TAILSCALE-IP>:8765` over Tailscale. All POST endpoints except
`/health` require `Authorization: Bearer <token>`.

### GET /health (no auth, minimal)

```bash
curl http://127.0.0.1:8765/health
```

```json
{"ok": true, "service": "hydra-daemon", "version": "0.4.0",
 "uptime_seconds": 123.4, "jobs_running": 1, "jobs_finished": 3,
 "video_tools": {"kdenlive": true, "melt": true,
                 "ffmpeg": true, "ffprobe": true},
 "tailscale": {"present": true, "connected": true,
               "ip": "100.74.209.52"}}
```

The `tailscale` fields (v0.4) degrade honestly when Tailscale is
missing or down (`{"present": false, "connected": false, "ip":
null}`); `/health` never fails because of Tailscale.

### POST /exec

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command": "ls -la", "cwd": ".", "timeout_seconds": 30}' \
  http://127.0.0.1:8765/exec
```

Request: `command` (string, required), `cwd` (relative path inside
workspace, default `.`), `timeout_seconds` (default 30, max 300),
`sandbox` (bool, default false), `sandbox_no_network` (bool, default
true; only meaningful with `sandbox`).

Response:

```json
{
  "ok": true,
  "sandboxed": false,
  "stdout": "total 8\n...",
  "stderr": "",
  "returncode": 0,
  "timed_out": false,
  "truncated": false
}
```

Blocked commands return `403` with `{"ok": false, "blocked": true,
"error": "blocked: ..."}` and nothing runs. `"sandbox": true` without
bubblewrap installed returns `501` naming bubblewrap.

### POST /read

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "notes/todo.txt", "start_line": 10, "end_line": 20}' \
  http://127.0.0.1:8765/read
```

`start_line`/`end_line` are 1-based and inclusive; either may be
omitted (open-ended). Without them the whole file is returned, exactly
as in v0.1. Response: `{"ok": true, "path": "...", "content": "...",
"truncated": false, "size": 123, "encoding": "utf-8-replace",
"start_line": 10, "end_line": 20, "lines_returned": 11,
"total_lines": 42}`. `total_lines` is null when the read was truncated
by the size cap. Invalid ranges return 400. Text only; binary files
decode with replacement characters.

### POST /write

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "hello.txt", "content": "hi\n", "mode": "overwrite"}' \
  http://127.0.0.1:8765/write
```

`mode` is `overwrite` (default) or `append`. Parent directories inside
the workspace are created. Response includes `bytes_written`.

### POST /edit (v0.2)

Patch-style exact replacement, OpenCode-`edit` inspired (no fuzzy
matching, no replacer chain — exactness is the safety property here).

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "app.py", "old_string": "return 1", "new_string": "return 2",
       "dry_run": false, "replace_all": false}' \
  http://127.0.0.1:8765/edit
```

- Zero matches -> 400, nothing written. More than one match ->
  409 with `matches` count, nothing written, unless
  `"replace_all": true`.
- `"dry_run": true` returns `matches`, `would_replace`, byte counts
  and context previews without writing.
- Files must decode as strict UTF-8 (binary -> 400); newlines are
  preserved byte-for-byte outside the replaced region.
- Response: `{"ok": true, "path": "...", "replaced": 1,
  "bytes_before": N, "bytes_after": M}`.

### POST /search (v0.2)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"pattern": "TODO", "path": ".", "glob": "*.py",
       "fixed_strings": false, "case_sensitive": true,
       "max_results": 100}' \
  http://127.0.0.1:8765/search
```

Uses ripgrep (`rg --json`) when installed, else grep (`-r -n -H`).
`path` is jailed like `/read`. Response:
`{"ok": true, "pattern": "...", "backend": "ripgrep",
"matches": [{"path": "...", "line": 12, "column": 5, "text": "..."}],
"match_count": 3, "truncated": false}`. `column` is null for the grep
backend. Backend differences are documented in `hydra/search.py`
(rg respects `.gitignore` and skips hidden files; grep does not).
The audit log records only the pattern's length and hash, never the
pattern itself. Invalid regex / bad glob -> 400.

### POST /jobs/start, /jobs/poll, /jobs/kill, /jobs/list (v0.2, persistent in v0.3)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"command": "make -j4", "cwd": ".", "timeout_seconds": 3600,
       "sandbox": true, "detached": true}' http://127.0.0.1:8765/jobs/start
# -> {"ok": true, "job_id": "a1b2c3d4e5f60718", "status": "running",
#     "sandboxed": true, "detached": true, "timeout_seconds": 3600}

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"job_id": "a1b2c3d4e5f60718", "offset": 0}' \
  http://127.0.0.1:8765/jobs/poll
# -> {"ok": true, "job_id": "...", "status": "running",
#     "stdout": "...", "stdout_offset": 0, "stdout_total_len": 123,
#     "stderr": "...", "returncode": null, "timed_out": false,
#     "truncated": false, "sandboxed": true, "detached": true,
#     "pid": 12345, "recovered": false,
#     "started_at": ..., "finished_at": null}
```

- Start validates the command with the same jail + blocklist +
  sandbox rules as `/exec` (refusals happen at start, before any
  process spawns). Max 32 jobs; a 33rd returns 429.
- Poll with `offset` to fetch only new stdout since the last poll.
  `stderr` is the full captured stderr (same 200KB cap as stdout).
  Terminal statuses:
  `done`, `failed` (nonzero exit), `killed` (kill/timeout; `timed_out`
  distinguishes the two).
- `/jobs/kill` SIGKILLs the whole process group — including for
  re-attached jobs after a restart. `/jobs/list` returns one-line
  summaries of all known jobs (finished jobs are pruned oldest-first
  when the cap is hit; pruning deletes the database row and log files).
- Job output is capped at 200KB per stream at read time, like sync
  exec; log files are hard-capped at 1MB per stream.
- v0.3 persistence: records live in `jobs.db` + `job-logs/` under
  the data dir. A `"detached": true` job survives daemon shutdown
  and restarts and is re-attached (`recovered: true`) on the next
  start. A re-attached job found dead reports `done` with
  `returncode: null` (the exit code is unknowable — never faked).

### POST /list

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": ".", "recursive": false}' http://127.0.0.1:8765/list
```

Response: `{"ok": true, "path": ".", "entries": [{"name": "hello.txt",
"type": "file", "size": 3}], "truncated": false}`. Capped at 500 entries.

### POST /memory/add

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"text": "Baji laptop is Arch Linux with KDE Plasma",
       "kind": "fact", "source": "screenshot 2026-11-15",
       "entities": [{"name": "Baji", "type": "person"},
                    {"name": "Arch Linux", "type": "os"}],
       "relations": [{"subject": "Baji", "predicate": "uses",
                      "object": "Arch Linux"}]}' \
  http://127.0.0.1:8765/memory/add
```

Response: `{"ok": true, "id": 1}`.

### POST /memory/search

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"query": "Arch Linux laptop", "limit": 10}' \
  http://127.0.0.1:8765/memory/search
```

Response: `{"ok": true, "query": "...", "fts_available": true,
"results": [{"id": 1, "text": "...", "kind": "fact", ...}],
"graph": {"Baji": [{"subject": "Baji", "predicate": "uses",
"object": "Arch Linux", ...}]}}`.

Search is keyword (FTS5 `MATCH` on sanitized terms) + 1-hop neighbours
for entities named in the matched texts. If FTS5 is unavailable,
`fts_available` is false and LIKE matching is used instead.

### POST /memory/graph/add

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"subject": "Baji", "predicate": "uses", "object": "KDE Plasma"}' \
  http://127.0.0.1:8765/memory/graph/add
```

Endpoints are auto-created as entities. Response: `{"ok": true, "id": N}`
(`id` may be null when the relation already existed).

### POST /memory/graph/neighbors

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"entity": "Baji"}' \
  http://127.0.0.1:8765/memory/graph/neighbors
```

Returns relations where the entity is subject or object (both directions).

### POST /memory/delete, /memory/list (v0.2)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"id": 3}' http://127.0.0.1:8765/memory/delete
# -> {"ok": true, "deleted": true, "id": 3}
# Or delete a graph relation: {"relation_id": 5}. Unknown id -> 404.
# Deleting a memory also removes graph relations sourced from it.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"limit": 20, "kind": "fact"}' http://127.0.0.1:8765/memory/list
# -> {"ok": true, "memories": [...newest first...], "count": N,
#     "total": M, "kind": "fact"}
```

### POST /screen/shot (v0.2, best-effort GUI)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"save_to": "shots/shot.png"}' http://127.0.0.1:8765/screen/shot
```

Tries spectacle, then grim, then the xdg-desktop-portal Screenshot
method over D-Bus. With `save_to` (workspace-relative) the PNG is
written inside the workspace and the response is
`{"ok": true, "path": "...", "backend": "spectacle", "width": 1920,
"height": 1080, "bytes": 123456}`; without it, the same fields plus
`data_base64`. Any failure returns 501 with an error naming every
backend tried and what was missing — success is never faked.
Requires the daemon to run inside the graphical session. See
`docs/GUI_SETUP.md`.

### POST /input/move, /input/click, /input/type (v0.2, best-effort GUI)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"x": 640, "y": 360}' http://127.0.0.1:8765/input/move
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"x": 640, "y": 360, "button": "left", "clicks": 2}' \
  http://127.0.0.1:8765/input/click
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"text": "hello"}' http://127.0.0.1:8765/input/type
```

All three shell out to `ydotool` (which needs `ydotoold` running and
the user in the `input` group). Missing ydotool -> 501 naming it.
Bad arguments -> 400. Typed text is capped at 4000 chars and is never
written to the audit log (length + hash only). See `docs/GUI_SETUP.md`.

### POST /system/info (v0.3, read-only)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{}' http://127.0.0.1:8765/system/info
# -> {"ok": true,
#     "disk": {"path": "/home/baji/hydra-workspace", "total_bytes": N,
#              "used_bytes": N, "free_bytes": N},
#     "load_average": [0.5, 0.4, 0.3],
#     "memory": {"total_bytes": N, "available_bytes": N, "free_bytes": N},
#     "uptime_seconds": 12345.6,
#     "tools": {"rg": true, "bwrap": true, "ffmpeg": true,
#               "ydotool": false, "spectacle": true, "grim": false}}
```

Pure reads; unavailable fields are `null`. No subprocess, no network.

### POST /git/status (v0.3, read-only)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "."}' http://127.0.0.1:8765/git/status
# -> {"ok": true, "is_repo": true, "path": "/home/baji/hydra-workspace",
#     "branch": "main", "detached": false, "head": "abc123...",
#     "upstream": "origin/main", "ahead": 1, "behind": 0,
#     "changes": [{"status": "M", "path": "app.py"}], "change_count": 1,
#     "truncated": false}
```

`path` is jailed to the workspace (escape -> 403, missing -> 404).
A directory that is not a repo returns `is_repo: false` (a normal
answer, not an error). No upstream configured -> `upstream`/`ahead`/
`behind` are `null`. Changes are capped at 200 entries. Missing git
-> 501. No commit/push/fetch behaviour exists in this endpoint.

### POST /system/tailscale (v0.4, read-only)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{}' http://127.0.0.1:8765/system/tailscale
# -> {"ok": true, "connected": true, "backend_state": "Running",
#     "self_hostname": "arch", "self_ip": "100.74.209.52",
#     "version": "1.104.7", "peer_count": 2}
```

Runs `tailscale status --json` (fixed argv, scrubbed env) and
returns only the six safe fields above. No node keys, no peer
list, no peer identities — they are dropped during parsing, and
the audit entry records `connected` + `peer_count` only.
Installed-but-not-logged-in is a normal 200 with
`connected: false` (e.g. `backend_state: "NeedsLogin"`).
Missing `tailscale` binary -> 501; `tailscaled` down or
unparseable output -> 502. See `docs/TAILSCALE_AUTO.md`.

### POST /system/doctor (v0.4, read-only, no auto-fix)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{}' http://127.0.0.1:8765/system/doctor
# -> {"ok": true, "bind": "100.74.209.52", "bind_mode": "tailscale",
#     "port": 8765, "workspace": "/home/baji/hydra-workspace",
#     "data_dir": "/home/baji/.local/share/hydra",
#     "checks": [{"name": "tailscale_present", "status": "pass",
#                 "detail": "tailscale is installed.",
#                 "fix_hint": null}, ...],
#     "summary": {"pass": 12, "warn": 1, "fail": 0},
#     "overall": "warn"}
```

Checks: `tailscale_present`, `tailscale_connected`, `tool_melt`,
`tool_ffmpeg`, `tool_ffprobe`, `tool_kdenlive`, `tool_bwrap`,
`tool_rg`, `disk_free` (fail below 1 GiB, warn below 5 GiB),
`token_permissions` (mode exactly 0600; contents never read),
`workspace`, `systemd_unit` (read-only `systemctl --user`
queries), `bind_mode`. Each check carries a `fix_hint` naming the
command Baji runs by hand — the doctor never fixes anything. The
audit entry records `overall` + the summary only. See
`docs/TAILSCALE_AUTO.md` for the full check table and the live
verification checklist.

### POST /browser/status, /browser/navigate, /browser/evaluate, /browser/content, /browser/shot, /browser/click, /browser/type (v0.3, loopback-only CDP)

Chrome must be running with `--remote-debugging-port=9222` (see
`scripts/chrome-debug.sh` and `docs/BROWSER_SETUP.md`). Every call
takes optional `host` (default `127.0.0.1`), `port` (default 9222)
and `target_id` (default: first page target). A non-loopback host
returns 403 before any socket opens. An unreachable Chrome returns
501 naming the launch script (except `/browser/status`, which is a
probe: it returns 200 with `available: false`). A CDP protocol
failure returns 502.

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"port": 9222}' http://127.0.0.1:8765/browser/status
# -> {"ok": true, "available": true, "browser": "Chrome/...",
#     "protocol_version": "1.3", "targets": [{"id": "...",
#     "type": "page", "title": "...", "url": "..."}], "target_count": 1}

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"url": "https://example.com"}' http://127.0.0.1:8765/browser/navigate
# -> {"ok": true, "url": "https://example.com", "frame_id": "...",
#     "target_id": "..."}
# Navigation allows http/https + about:blank only; file:/data:/chrome:
# URLs return 400.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"expression": "document.title"}' http://127.0.0.1:8765/browser/evaluate
# -> {"ok": true, "value": "\"Example\"", "value_json": true,
#     "truncated": false, "target_id": "..."}
# The value is JSON-serialised (200KB cap, truncated flag). A page
# exception returns {"value": null, "exception": "..."} instead.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{}' http://127.0.0.1:8765/browser/content
# -> {"ok": true, "url": "...", "title": "...", "text": "...",
#     "html": "...", "text_truncated": false, "html_truncated": false,
#     "target_id": "..."}   (text/html capped at 100KB each)

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"save_to": "shots/page.png"}' http://127.0.0.1:8765/browser/shot
# -> {"ok": true, "path": "shots/page.png", "backend": "cdp",
#     "width": 1280, "height": 720, "bytes": 12345}
# Without save_to, the same fields plus data_base64 (PNG verified).

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"x": 100, "y": 200}' http://127.0.0.1:8765/browser/click
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"text": "hello"}' http://127.0.0.1:8765/browser/type
# Click dispatches mousePressed + mouseReleased at viewport coords.
# Type inserts text (4000-char cap, never audit-logged).
```

Audit entries for browser calls carry lengths/hashes only — never
URLs, expressions, page content, or typed text.

### POST /video/probe, /video/project/create, /video/render/start, /video/render/poll, /video/preview, /video/list (v0.4, headless MLT/melt)

Headless video only: no GUI is ever opened. Profiles:
`1080p30` (1920x1080), `1080p30-vertical` (1080x1920), `720p30`,
`1080p25`. Render codec is fixed (libx264 CRF 18 + AAC 192k); no
caller-supplied render flags exist. All paths are workspace-jailed
(escape -> 403). Missing tools -> 501 naming the Arch package;
disk-guard refusal -> 507.

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "clips/a.mp4"}' http://127.0.0.1:8765/video/probe
# -> {"ok": true, "path": "clips/a.mp4", "duration": 12.5,
#     "width": 1920, "height": 1080, "fps": 30.0,
#     "video_codec": "h264", "audio_codec": "aac",
#     "has_video": true, "has_audio": true, "streams": [...]}
# ffprobe missing -> 501. Unprobeable file -> 400.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"name": "demo", "profile": "1080p30",
       "tracks": [{"type": "video", "clips": [
         {"asset": "clips/a.mp4", "in": 0, "out": 5},
         {"kind": "title", "text": "Hello", "duration": 3,
          "font_size": 64}]},
        {"type": "audio", "clips": [
         {"asset": "music.mp3", "in": 0, "out": 8, "volume": 0.5}]}],
       "subtitle": "subs/caps.srt",
       "assets": [{"path": "clips/a.mp4", "source": "phone recording",
                   "license": "own"}]}' \
  http://127.0.0.1:8765/video/project/create
# -> {"ok": true, "project_id": "demo", "dir": "video/demo",
#     "timeline_path": "video/demo/timeline.json",
#     "manifest_path": "video/demo/manifest.json",
#     "mlt_path": "video/demo/project.mlt", "profile": "1080p30",
#     "duration": 8.0, "track_count": 2, "clip_count": 3,
#     "asset_count": 2, "subtitle": "subs/caps.srt"}
# Writes timeline.json + manifest.json + project.mlt. "start"
# defaults to appending after the previous clip on the track;
# overlapping clips on one track -> 400. Existing project -> 409
# unless "overwrite": true. Crossfades go in "transitions":
# [{"type": "crossfade", "a_track": 0, "b_track": 1,
#    "start": 4.5, "duration": 1.0}] (overlapping clips must sit on
# adjacent tracks). Titles need a video track. Caps: 8 tracks,
# 200 clips/track, 500 clips total, 500-char titles.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"project_id": "demo", "dry_run": true}' \
  http://127.0.0.1:8765/video/render/start
# -> {"ok": true, "dry_run": true, "project_id": "demo",
#     "profile": "1080p30", "duration": 8.0,
#     "output_path": "video/demo/output.mp4",
#     "estimated_bytes": 123456, "free_bytes": 4567890,
#     "melt_available": true}
# Without dry_run, creates + starts a workflow:
# -> {"ok": true, "workflow_id": "...", "project_id": "demo",
#     "status": "running", "current_step": "ingest",
#     "progress_percent": 0, "output_path": "video/demo/output.mp4",
#     "steps": [...], ...}
# "output" overrides the default output path (jailed).
# "timeout_seconds" default 14400 (4h), max 86400. Melt missing ->
# 501 before any workflow is created. Disk guard -> 507.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"workflow_id": "..."}' http://127.0.0.1:8765/video/render/poll
# -> {"ok": true, "workflow_id": "...", "status": "running",
#     "current_step": "render", "progress_percent": 30,
#     "steps": [{"step": "ingest", "status": "done", "attempts": 1,
#                "error": null, "detail": {...}}, ...],
#     "warnings": [], "preview_paths": [], "error": null, ...}
# Status: pending|queued|running|done|failed|interrupted|cancelled.
# Progress: 5/15/30/75/90/100 as steps complete. Unknown id -> 404.
# {"workflow_id": "...", "resume": true} resumes an interrupted or
# failed workflow from its last completed step (409 otherwise).

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "video/demo/output.mp4", "timestamps": [1.0, 4.0]}' \
  http://127.0.0.1:8765/video/preview
# -> {"ok": true, "path": "video/demo/output.mp4",
#     "frames": [{"timestamp": 1.0, "path": "video/demo/previews/frame_0001.png",
#                 "bytes": 12345}, ...],
#     "paths": ["video/demo/previews/frame_0001.png", ...]}
# Without timestamps, frames are taken at 10/50/90% of duration
# (ffprobe). "output_dir" overrides the default previews dir
# (jailed). Max 10 timestamps. ffmpeg missing -> 501. File paths
# only — never image bytes.

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{}' http://127.0.0.1:8765/video/list
# -> {"ok": true, "projects": [{"project_id": "demo", ...}],
#     "workflows": [{"workflow_id": "...", "status": "done", ...}],
#     "render_busy": false}
```

`/shutdown` now also interrupts running video workflows
(response adds `workflows_interrupted`); interrupted workflows
stay resumable after the next start.

### POST /shutdown

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"confirm": true}' http://127.0.0.1:8765/shutdown
```

Responds `{"ok": true, "message": "shutting down", "jobs_killed": N}`
then stops the server (running NON-detached jobs are killed first;
detached jobs keep running on purpose). Requires `confirm: true`
exactly.

## Error shape

All errors: `{"ok": false, "error": "<human-readable reason>"}` with an
appropriate HTTP status (400 bad request, 401 auth, 403 jail/blocklist/
non-loopback CDP, 404 not found / unknown endpoint, 409 ambiguous edit /
existing project / unresumable workflow, 413 body too large, 429 too
many jobs, 501 missing optional tool/sandbox/GUI backend / Chrome /
melt / tailscale binary, 502 CDP command failed / tailscaled down or
unparseable, 507 disk guard refused a render). A 501
always names what is missing and how to install or start it.

## Configuration

Precedence: CLI flags > `HYDRA_*` env vars > JSON config file > defaults.
See `config.example.json`. The bearer token is NEVER in the config file;
it lives only in the 0600 token file. The `bind` value may be an
address (`127.0.0.1`, a Tailscale IP) or the word `tailscale`,
which resolves the current Tailscale IPv4 at startup and fails
closed when Tailscale is down (see `docs/TAILSCALE_AUTO.md`).

## What v0.3 deliberately leaves out

- No HTTPS on the daemon (Tailscale encrypts the transport).
- No approval queue per request (start/stop is the control).
- No sandbox/container isolation by default (opt-in bubblewrap only,
  no CPU/RAM limits, no seccomp policy).
- No streaming responses. Outputs are truncated, not streamed.
- No agent loop, no subagents, no sessions on the laptop. Mikey in
  the cloud is the only loop; the daemon only executes requests.
- No cron/scheduling inside the daemon. Hermes Agent has a cron
  scheduler in its gateway; HYDRA deliberately does not: scheduled
  work, if ever wanted, must be requested by Mikey per run, so
  nothing ever starts on the laptop without a live request behind it.
- No credential handling or extraction of any kind. The daemon never
  reads browser profiles, keychains, or session stores; the CDP
  endpoints drive the browser Baji himself started, nothing more.
- No LSP, no sessions store, no model/embeddings, no telemetry.

## What v0.4 deliberately leaves out

- No GUI automation for video: no Kdenlive window control, no
  D-Bus scripting, no screen/input use in the video path. The
  pipeline is MLT XML + melt only.
- No `.kdenlive` project generation (no bin metadata, no Kdenlive
  effects catalogue). The output is plain MLT XML that Kdenlive
  can open; full round-trip fidelity is unverified.
- No keyframed effects, colour grading, motion/zoom, or nested
  timelines. No caller-supplied melt/ffmpeg flags (fixed consumer
  arguments only).
- No media downloading, no auto-scraper, no stock-footage search.
  Manifest URLs are provenance notes; media arrives through the
  existing file/shell endpoints with explicit URLs from Mikey.
- No automatic resume, no render queue persistence across
  different daemons, no parallel renders (one at a time, by
  design), no CPU/RAM limits on melt beyond the timeout.
- No `hydra_client.py` subcommands for `/video/*` yet (HTTP + curl
  examples in this file are the interface for v0.4).
- No Tailscale-auto overreach: no relay, no Cloudflare tunnel, no
  polling, no HTTPS termination, no public bind, no credential
  storage, no auto-update, no auto-fix in the doctor, no removal
  of the per-session token handoff, and no MagicDNS / Tailscale
  Serve claims (listed only as unverified future investigation in
  `docs/TAILSCALE_AUTO.md`). The daemon still makes zero outbound
  calls; the only new subprocesses are local read-only CLI
  queries (`tailscale`, `systemctl --user`).

## References that shaped v0.2

- OpenCode (`https://github.com/anomalyco/opencode`, MIT): the
  edit/search/bash tool split, exact-match edit semantics, background
  task polling, and per-tool permission thinking informed `/edit`,
  `/search`, `/jobs/*` and the blocklist. OpenCode has no GUI control
  and no OS sandbox by default; HYDRA v0.2 goes further on both, and
  deliberately does not copy its agent loop, session store, or LSP.
- Hermes Agent (`https://github.com/NousResearch/hermes-agent`, MIT):
  the terminal-backend idea (local/Docker/SSH), background-process
  management, and the candid "the OS is the only security boundary"
  stance informed the sandbox option and the honesty in SECURITY.md.
  HYDRA copies none of its agent loop, browser/computer-use tools,
  skills loop, or LLM summarization — the daemon stays a dumb
  executor, per Baji's constraint.

## References that shaped v0.3

- OpenCode (`https://github.com/anomalyco/opencode`, MIT): the
  long-lived server + persistent session model (sessions as durable
  rows, pending work surviving restarts) informed the persistent job
  store: HYDRA's `jobs.db` + log files + re-attach play the same
  role for background work that OpenCode's session store plays for
  conversations. HYDRA still copies no agent loop, no subagents, no
  sessions, no LSP.
- Hermes Agent (`https://github.com/NousResearch/hermes-agent`, MIT)
  and its docs (`https://hermes-agent.nousresearch.com/docs/
  user-guide/features/tools`, `.../docs/reference/tools-reference`):
  the background-process model (`terminal(background=true)` returning
  a session id, plus list/poll/wait/log/kill) informed detached jobs
  and their poll/kill/list surface; the browser toolset
  (`browser_exec`/`browser_cdp` driving Chromium over CDP) informed
  the `/browser/*` endpoints and the choice of raw CDP over a
  heavier automation stack. HYDRA deliberately does NOT copy the
  gateway process, the cron scheduler, the learning loop, subagent
  delegation, or the browser vault/credential tooling — no agent
  loop and no credential handling on the laptop, per Baji's
  constraints.
- Chrome DevTools Protocol
  (`https://chromedevtools.github.io/devtools-protocol/`): the
  protocol reference for Page/Runtime/Input domains used by
  `hydra/browser.py`. The WebSocket client (`hydra/websocket.py`)
  implements RFC 6455 from scratch because the daemon is stdlib-only
  by design — a deliberate decision, documented here so it is not
  mistaken for an oversight.

## References that shaped v0.4

- kdenlive-mcp by mhbxyz (`https://github.com/mhbxyz/kdenlive-mcp`):
  the typed timeline model, ffprobe-based media diagnostics, preset
  rendering, and render verification ideas, plus the Linux-only
  Kdenlive + melt + ffprobe toolchain shape. HYDRA copies no code
  and adds no MCP server: the daemon stays a plain HTTP dumb
  executor, and the video surface is six endpoints, not an agent
  toolkit.
- cli-anything-kdenlive (JSON project format + MLT XML generation
  for Kdenlive/melt, part of HKUDS/CLI-Anything): the JSON-spec ->
  MLT XML -> `melt` render shape that v0.4 follows. HYDRA's
  timeline spec and XML generator are its own small implementation,
  not a port.
- mlt-python (pure-Python MLT XML library): the producer/playlist/
  tractor element structure. HYDRA reimplements the subset it needs
  with stdlib `xml.etree` instead of depending on it.
- MLT XML documentation (`https://www.mltframework.org/docs/mltxml/`):
  the serialization format (producers first, playlists per track,
  tractor + multitrack + transitions, clock time values) and the
  `melt project.mlt -consumer avformat:output.mp4` render shape.
