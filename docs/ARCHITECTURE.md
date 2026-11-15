# ARCHITECTURE.md — HYDRA v0.3

## Design in one paragraph

Mikey (cloud) thinks. The laptop daemon executes. They talk over
HTTP+JSON on the private Tailscale network, authenticated with a
bearer token. The daemon holds no model, makes no outbound calls,
and keeps only four kinds of local state: the workspace files, an
append-only audit log, a small SQLite memory database, and a job
database + log files. Everything else is request/response and
forgotten. v0.2 added patch edits, code search, background jobs, an
opt-in bubblewrap sandbox, and best-effort GUI endpoints (screenshot
+ input). v0.3 adds persistent + detached jobs, resilience (PID file,
restart-friendly unit, richer `/health`), read-only system/git
endpoints, and loopback-only CDP browser control — all still dumb
execution, no agent loop on the laptop.

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

Memory flow is the same shape, except the store is SQLite and the
search is FTS5 keyword + graph neighbours (no vectors, no model).

GUI flow: `/screen/shot` tries spectacle, then grim, then the
xdg-desktop-portal Screenshot call over D-Bus; the first success
wins and the PNG is verified (magic bytes + IHDR size) before it is
returned. `/input/*` shells out to `ydotool` only. Any failure is a
501 naming the missing tool/permission — success is never faked.

## Endpoints

Base URL (examples): `http://127.0.0.1:8765` locally, or
`http://<TAILSCALE-IP>:8765` over Tailscale. All POST endpoints except
`/health` require `Authorization: Bearer <token>`.

### GET /health (no auth, minimal)

```bash
curl http://127.0.0.1:8765/health
```

```json
{"ok": true, "service": "hydra-daemon", "version": "0.3.0",
 "uptime_seconds": 123.4, "jobs_running": 1, "jobs_finished": 3}
```

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
non-loopback CDP, 404 not found / unknown endpoint, 409 ambiguous edit,
413 body too large, 429 too many jobs, 501 missing optional tool/sandbox/
GUI backend / Chrome, 502 CDP command failed). A 501 always names what
is missing and how to install or start it.

## Configuration

Precedence: CLI flags > `HYDRA_*` env vars > JSON config file > defaults.
See `config.example.json`. The bearer token is NEVER in the config file;
it lives only in the 0600 token file.

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
