# ARCHITECTURE.md — HYDRA v0.2

## Design in one paragraph

Mikey (cloud) thinks. The laptop daemon executes. They talk over
HTTP+JSON on the private Tailscale network, authenticated with a
bearer token. The daemon holds no model, makes no outbound calls,
and keeps only three kinds of local state: the workspace files, an
append-only audit log, and a small SQLite memory database. Everything
else is request/response and forgotten. v0.2 adds patch edits, code
search, background jobs, an opt-in bubblewrap sandbox, and best-effort
GUI endpoints (screenshot + input) — all still dumb execution, no
agent loop on the laptop.

## Components

```text
hydra/daemon.py        ThreadingHTTPServer, routes, /exec runner, jobs wiring
hydra/security.py      jail + blocklist + token checks (audit this first)
hydra/memory_store.py  SQLite: memories + FTS5 + entities + relations
hydra/audit.py         JSONL audit log writer
hydra/search.py        ripgrep (preferred) / grep workspace search
hydra/jobs.py          background jobs: spawn, poll, kill, output caps
hydra/sandbox.py       bubblewrap argv builder (fails closed when missing)
hydra/gui.py           screenshots + ydotool input (clear errors, no fakes)
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
process to `JobManager`: reader threads drain stdout/stderr (200KB
caps per stream), a reaper enforces the deadline by killing the whole
process group, and `/jobs/poll` returns incremental output by byte
offset. Jobs die with the daemon (shutdown + atexit kill-all).

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
{"ok": true, "service": "hydra-daemon", "version": "0.2.0"}
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

### POST /jobs/start, /jobs/poll, /jobs/kill, /jobs/list (v0.2)

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"command": "make -j4", "cwd": ".", "timeout_seconds": 3600,
       "sandbox": true}' http://127.0.0.1:8765/jobs/start
# -> {"ok": true, "job_id": "a1b2c3d4e5f60718", "status": "running",
#     "sandboxed": true, "timeout_seconds": 3600}

curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"job_id": "a1b2c3d4e5f60718", "offset": 0}' \
  http://127.0.0.1:8765/jobs/poll
# -> {"ok": true, "job_id": "...", "status": "running",
#     "stdout": "...", "stdout_offset": 0, "stdout_total_len": 123,
#     "stderr": "...", "returncode": null, "timed_out": false,
#     "truncated": false, "started_at": ..., "finished_at": null}
```

- Start validates the command with the same jail + blocklist +
  sandbox rules as `/exec` (refusals happen at start, before any
  process spawns). Max 32 jobs; a 33rd returns 429.
- Poll with `offset` to fetch only new stdout since the last poll.
  `stderr` is the full captured stderr (same 200KB cap as stdout).
  Terminal statuses:
  `done`, `failed` (nonzero exit), `killed` (kill/timeout; `timed_out`
  distinguishes the two).
- `/jobs/kill` SIGKILLs the whole process group. `/jobs/list`
  returns one-line summaries of all known jobs (finished jobs are
  pruned oldest-first when the cap is hit).
- Job output is capped at 200KB per stream, like sync exec.

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

### POST /shutdown

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"confirm": true}' http://127.0.0.1:8765/shutdown
```

Responds `{"ok": true, "message": "shutting down", "jobs_killed": N}`
then stops the server (running jobs are killed first). Requires
`confirm: true` exactly.

## Error shape

All errors: `{"ok": false, "error": "<human-readable reason>"}` with an
appropriate HTTP status (400 bad request, 401 auth, 403 jail/blocklist,
404 not found / unknown endpoint, 409 ambiguous edit, 413 body too
large, 429 too many jobs, 501 missing optional tool/sandbox/GUI
backend). A 501 always names what is missing and how to install it.

## Configuration

Precedence: CLI flags > `HYDRA_*` env vars > JSON config file > defaults.
See `config.example.json`. The bearer token is NEVER in the config file;
it lives only in the 0600 token file.

## What v0.2 deliberately leaves out

- No HTTPS on the daemon (Tailscale encrypts the transport).
- No approval queue per request (start/stop is the control).
- No sandbox/container isolation by default (opt-in bubblewrap only,
  no CPU/RAM limits, no seccomp policy).
- No streaming responses. Outputs are truncated, not streamed.
- No browser/CDP control. The GUI endpoints can see the screen and
  move the mouse, but there is no DOM access, no tab control, no
  form filling by selector. A future version could add it with:
  Chrome/Chromium started with `--remote-debugging-port` (Baji
  starts it himself, localhost-only), plus a websocket client in the
  daemon (stdlib has no websocket client, so this needs either a
  vendored minimal client or a small dependency — a deliberate
  decision, not an accident). Until then, browser work goes through
  screenshots + clicks + typing, with Mikey reading the screen.

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
