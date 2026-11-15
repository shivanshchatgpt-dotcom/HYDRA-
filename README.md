# HYDRA v0.3 — Laptop Harness (Tailscale + Dumb Daemon)

> **Mikey (in the cloud) is the brain. This daemon is only the body.**
> The daemon never thinks, never calls a model, never goes on the internet.
> It only runs the exact file / shell / memory / screen / browser
> operations Mikey asks for, inside one workspace folder (plus opt-in
> screen/input control and loopback-only browser control), with a
> blocklist and an audit log.

## What this is

A small, auditable Python daemon that runs on Baji's laptop (Arch Linux,
KDE Plasma, Wayland) and gives Mikey a *virtual body* on that laptop:

- Mikey talks to the daemon over HTTP+JSON, through the **private
  Tailscale network** (never the public internet by default).
- The daemon executes: list files, read files (whole or line ranges),
  write files, patch-edit files, search code (ripgrep/grep), run shell
  commands (sync or as background jobs, optionally in a bubblewrap
  sandbox), store/search/delete small long-term memories, read system
  info and git status (read-only), and — when the optional GUI tools
  are installed — take screenshots and send mouse/keyboard input.
  When Chrome is running with remote debugging on loopback, it can
  also drive that Chrome over CDP (navigate, evaluate, read content,
  screenshot, click, type) — see `docs/BROWSER_SETUP.md`.
- Background jobs are **persistent**: records live in `jobs.db` with
  per-job log files, and **detached** jobs keep running across daemon
  restarts and are re-attached when the daemon comes back — so work
  continues without Baji present. See `docs/AUTONOMY.md` for exactly
  what runs without him and what still needs him.
- Every request needs a bearer token, stays inside the workspace jail,
  passes a visible command blocklist, and is written to an audit log
  (before the response is sent).

## What this is NOT

- **No model.** No Ollama, no local LLM, no embeddings, no LLM API calls
  from the daemon. Every mention of Ollama in this repo states only
  that it is **not used**.
- **No fallback.** If the daemon is stopped, or Tailscale is down, Mikey
  simply cannot reach the laptop. Nothing else takes over. That is the
  point: Baji stays in control with an obvious kill switch.
- **No agent loop, no scheduling.** The daemon never decides to do
  anything on its own, never runs cron-like jobs, never wakes itself
  up. Every action is a request from Mikey. What v0.3 adds is
  *persistence* (jobs and records survive restarts) and *resilience*
  (the opt-in systemd unit restarts a crashed daemon) — not autonomy
  of the daemon itself.
- **No telemetry, no public bind.** The daemon binds `127.0.0.1` by
  default, starts only when Baji starts it (or when he explicitly
  enables the opt-in systemd unit), and sends nothing anywhere on
  its own. Auto-start is never installed by setup; see
  `docs/AUTONOMY.md`.

## Architecture (one picture, in words)

```text
Baji's chat  ->  Mikey (cloud brain: thinks, plans, writes code)
                      |
                      |  HTTPS/HTTP+JSON + bearer token
                      |  over Tailscale private network (100.64.0.0/10)
                      v
            HYDRA daemon (laptop body: dumb executor)
                      |
                      +-- workspace jail: ~/hydra-workspace ONLY
                      +-- blocklist: sudo/rm -rf/curl|sh/... refused
                      +-- optional bubblewrap sandbox for exec/jobs
                      +-- persistent jobs: jobs.db + job-logs/
                      |   (detached jobs survive daemon restarts)
                      +-- GUI (opt-in tools): spectacle/grim screenshot,
                      |   ydotool mouse/keyboard — see docs/GUI_SETUP.md
                      +-- browser (opt-in): Chrome over CDP, loopback
                      |   only — see docs/BROWSER_SETUP.md
                      +-- read-only: /system/info, /git/status
                      +-- audit log: ~/.local/share/hydra/audit.jsonl
                      +-- PID file: ~/.local/share/hydra/hydra.pid
                      +-- memory: ~/.local/share/hydra/memory.db
                          (SQLite FTS5 keyword + graph, NO vectors)
```

Full request/response examples: see `docs/ARCHITECTURE.md`.
Threat model and revocation: see `SECURITY.md`.

## Requirements

- Arch Linux (tested target: Baji's ASUS TUF Gaming F16, Arch, KDE 6.7.4,
  Wayland, kernel 7.1.10-arch1-1). The daemon itself is plain Python and
  should run on any Linux with Python 3.11+.
- Python 3.11+ (stdlib only, **no pip packages**).
- Tailscale (only needed for Mikey to reach the laptop; localhost testing
  works without it).
- Optional tools, each enabling one feature (the daemon works
  without them and returns a clear 501 naming the missing tool):
  `ripgrep` (fast `/search`), `bubblewrap` (sandboxed exec),
  `spectacle` or `grim` (screenshots), `ydotool` + `ydotoold`
  (mouse/keyboard input), `git` (`/git/status`),
  `google-chrome`/`chromium` with `--remote-debugging-port=9222`
  (`/browser/*` endpoints). See `docs/GUI_SETUP.md` and
  `docs/BROWSER_SETUP.md` for exact setup.

## Setup (on the laptop, run by Baji)

```bash
cd ~/hydra-harness
./scripts/install-arch.sh
```

That script only checks tools, creates directories, generates a 0600
token file if missing, and prints next steps. It never runs `sudo`,
never installs packages, never changes the firewall, never enables
auto-start.

Then, in one terminal, start the daemon (localhost-only test first):

```bash
./scripts/start-daemon.sh
```

In a second terminal, test it:

```bash
curl http://127.0.0.1:8765/health
python3 hydra_client.py list --path .
python3 hydra_client.py exec --command "pwd && ls -la"
python3 hydra_client.py search --pattern "TODO"
python3 hydra_client.py screen-shot --save-to shots/shot.png
```

To let Mikey reach it over Tailscale, find the laptop's Tailscale IP and
restart the daemon bound to it (see `scripts/install-arch.sh` output):

```bash
tailscale ip -4
./scripts/start-daemon.sh --bind <TAILSCALE-IP> --port 8765
```

Mikey's side then connects through the Tailscale tunnel proxy to
`http://<TAILSCALE-IP>:8765` with the bearer token. Baji must share
nothing except confirming the daemon is running; the token itself must
never be pasted into chat. (How Mikey obtains the token for a session is
arranged separately through the approved credential flow, never chat.)

## Kill switch (stop everything, instantly)

Any one of these stops Mikey's access completely:

1. `Ctrl-C` in the daemon terminal, or
2. `python3 hydra_client.py shutdown` (needs the token), or
3. `systemctl --user stop hydra` (only if you installed the unit), or
4. `sudo tailscale down` (cuts the private network entirely).

To fully revoke: stop the daemon, rotate the token
(`python3 scripts/generate_token.py --force`), and optionally delete
`~/.local/share/hydra/`. See `SECURITY.md`.

> **Detached jobs keep running after the daemon stops.** The kill
> switch above stops Mikey's *access* instantly, but a detached job
> (started with `"detached": true`) is a normal process in its own
> session: it finishes on its own, or you kill it with
> `python3 hydra_client.py jobs-kill --job-id <id>` before stopping
> the daemon, or with plain `ps`/`kill` afterwards. `/jobs/list`
> shows what is running. See `docs/AUTONOMY.md`.

## What's new in v0.3

- Persistent + detached jobs: `jobs.db` + per-job log files, jobs
  survive daemon restarts (re-attach by PID liveness, guarded against
  PID reuse), `"detached": true` survives daemon shutdown.
- Resilience: systemd unit with `Restart=always` (opt-in, never
  auto-enabled), daemon PID file, `/health` gains `uptime_seconds`
  and job counts.
- Read-only endpoints: `/system/info` (disk/load/memory/uptime/tools)
  and `/git/status` (branch, ahead/behind, short status; workspace-only).
- CDP browser control, loopback only: `/browser/status|navigate|
  evaluate|content|shot|click|type`, with a hand-rolled stdlib
  WebSocket client (no new dependencies). See `docs/BROWSER_SETUP.md`.
- `docs/AUTONOMY.md`: what runs without Baji present, what still
  needs him (laptop on/awake, daemon running, token handoff per new
  chat session, Chrome in debug mode for browser work).

Full list: see `CHANGELOG.md`. What still needs live verification on
the laptop (CDP against real Chrome, autostart unit, detached jobs
across a real restart, GUI endpoints): see `docs/BROWSER_SETUP.md`,
`docs/AUTONOMY.md`, and `docs/GUI_SETUP.md`.

## Memory (small RAG + graph, keyword only)

The daemon keeps a small SQLite memory store for facts Baji/Mikey
explicitly save, e.g. "Baji's laptop is Arch Linux, KDE Plasma".

- Search is **keyword** search (SQLite FTS5) plus **1-hop graph
  neighbours** (entities + relations). There are no embeddings and no
  semantic/vector search, because no local model is allowed.
- All memory access goes through authenticated endpoints and is
  audit-logged (lengths/hashes, not full text).
- The database lives at `~/.local/share/hydra/memory.db`. Delete that
  file to wipe daemon-side memory. It is never synced anywhere.

## Uninstall

```bash
# Stop it
# Ctrl-C in the daemon terminal, or:
systemctl --user disable --now hydra 2>/dev/null || true
rm -f ~/.config/systemd/user/hydra.service

# Remove data (workspace files are yours; delete only if you want to)
rm -rf ~/.local/share/hydra ~/.config/hydra
# rm -rf ~/hydra-workspace   # only if you want your workspace files gone
# rm -rf ~/hydra-harness     # the code itself
```

## History

HYDRA was originally a planning document for an AI clipping system.
The original README is preserved verbatim at
`docs/CLIPPING_PLAN_ARCHIVED.md` (archived 2026-11-15, when HYDRA was
repurposed into this harness). Nothing was deleted from git history.

## Files

| Path | What it is |
|---|---|
| `hydra/daemon.py` | HTTP API + exec/read/write/edit/search/jobs/GUI/browser/system/git/shutdown |
| `hydra/security.py` | Workspace jail + command blocklist + token checks |
| `hydra/memory_store.py` | SQLite FTS5 + graph memory (no model) |
| `hydra/audit.py` | Append-only JSONL audit log |
| `hydra/search.py` | ripgrep/grep workspace search |
| `hydra/jobs.py` | Persistent + detached background jobs (jobs.db + log files) |
| `hydra/sandbox.py` | bubblewrap argv builder (never silently unsandboxed) |
| `hydra/gui.py` | Screenshots (spectacle/grim/portal) + ydotool input |
| `hydra/websocket.py` | Stdlib-only WebSocket client (RFC 6455, for CDP) |
| `hydra/browser.py` | CDP browser control, loopback only |
| `hydra/sysinfo.py` | Read-only system info + git status |
| `hydra_client.py` | Stdlib test client |
| `scripts/generate_token.py` | 0600 token generator |
| `scripts/install-arch.sh` | Arch setup checks + dirs + token |
| `scripts/start-daemon.sh` | Foreground start (manual only) |
| `scripts/chrome-debug.sh` | Launch Chrome with remote debugging (never kills Chrome) |
| `systemd/hydra.service` | Opt-in user unit, Restart=always (NOT enabled by setup) |
| `config.example.json` | Example config (no secrets in it) |
| `tests/test_daemon.py` | v0.1 stdlib unittest suite (30 tests) |
| `tests/test_v02.py` | v0.2 stdlib unittest suite |
| `tests/test_v03.py` | v0.3 stdlib unittest suite |
| `tests/fake_cdp.py` | Fake CDP server for browser/WebSocket tests |
| `docs/ARCHITECTURE.md` | Design + endpoint examples |
| `docs/GUI_SETUP.md` | GUI/sandbox laptop setup + live verification |
| `docs/BROWSER_SETUP.md` | Chrome debugging setup + live CDP checklist |
| `docs/AUTONOMY.md` | What runs without Baji, what still needs him |
| `CHANGELOG.md` | Version history |
| `SECURITY.md` | Threat model + rotation + revocation |
