# ARCHITECTURE.md — HYDRA v0.1

## Design in one paragraph

Mikey (cloud) thinks. The laptop daemon executes. They talk over
HTTP+JSON on the private Tailscale network, authenticated with a
bearer token. The daemon holds no model, makes no outbound calls,
and keeps only three kinds of local state: the workspace files, an
append-only audit log, and a small SQLite memory database. Everything
else is request/response and forgotten.

## Components

```text
hydra/daemon.py        ThreadingHTTPServer, routes, /exec runner
hydra/security.py      jail + blocklist + token checks (audit this first)
hydra/memory_store.py  SQLite: memories + FTS5 + entities + relations
hydra/audit.py         JSONL audit log writer
hydra_client.py        stdlib client (testing / manual use)
```

Data flow for one request:

1. Mikey sends `POST /exec` with `Authorization: Bearer <token>`.
2. Handler checks the token (constant-time compare). Fail -> 401 + audit.
3. Handler checks the jail (cwd inside workspace) and the blocklist.
   Fail -> 403 + audit. Nothing runs.
4. Command runs via `/bin/sh -c` with cwd forced inside the workspace,
   stdin closed, scrubbed env, timeout, output truncation.
5. JSON result returns. Audit entry appended (truncated command preview,
   lengths, exit code). No token, no secrets in the log.

Memory flow is the same shape, except the store is SQLite and the
search is FTS5 keyword + graph neighbours (no vectors, no model).

## Endpoints

Base URL (examples): `http://127.0.0.1:8765` locally, or
`http://<TAILSCALE-IP>:8765` over Tailscale. All POST endpoints except
`/health` require `Authorization: Bearer <token>`.

### GET /health (no auth, minimal)

```bash
curl http://127.0.0.1:8765/health
```

```json
{"ok": true, "service": "hydra-daemon", "version": "0.1.0"}
```

### POST /exec

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command": "ls -la", "cwd": ".", "timeout_seconds": 30}' \
  http://127.0.0.1:8765/exec
```

Request: `command` (string, required), `cwd` (relative path inside
workspace, default `.`), `timeout_seconds` (default 30, max 300).

Response:

```json
{
  "ok": true,
  "stdout": "total 8\n...",
  "stderr": "",
  "returncode": 0,
  "timed_out": false,
  "truncated": false
}
```

Blocked commands return `403` with `{"ok": false, "blocked": true,
"error": "blocked: ..."}` and nothing runs.

### POST /read

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "notes/todo.txt"}' http://127.0.0.1:8765/read
```

Response: `{"ok": true, "path": "...", "content": "...",
"truncated": false, "size": 123, "encoding": "utf-8-replace"}`.
Text only; binary files decode with replacement characters.

### POST /write

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"path": "hello.txt", "content": "hi\n", "mode": "overwrite"}' \
  http://127.0.0.1:8765/write
```

`mode` is `overwrite` (default) or `append`. Parent directories inside
the workspace are created. Response includes `bytes_written`.

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

### POST /shutdown

```bash
curl -H "Authorization: Bearer $HYDRA_TOKEN" \
  -d '{"confirm": true}' http://127.0.0.1:8765/shutdown
```

Responds `{"ok": true, "message": "shutting down"}` then stops the
server. Requires `confirm: true` exactly.

## Error shape

All errors: `{"ok": false, "error": "<human-readable reason>"}` with an
appropriate HTTP status (400 bad request, 401 auth, 403 jail/blocklist,
404 not found / unknown endpoint, 413 body too large).

## Configuration

Precedence: CLI flags > `HYDRA_*` env vars > JSON config file > defaults.
See `config.example.json`. The bearer token is NEVER in the config file;
it lives only in the 0600 token file.

## What v0.1 deliberately leaves out

- No HTTPS on the daemon (Tailscale encrypts the transport).
- No approval queue per request (start/stop is the control).
- No sandbox/container isolation.
- No screen/GUI control, no file watching, no background jobs API.
  Long commands run synchronously up to the timeout; use the timeout
  and polling via /list and /read for now.
- No streaming responses. Outputs are truncated, not streamed.
