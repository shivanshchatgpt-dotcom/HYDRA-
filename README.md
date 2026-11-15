# HYDRA v0.1 — Laptop Harness (Tailscale + Dumb Daemon)

> **Mikey (in the cloud) is the brain. This daemon is only the body.**
> The daemon never thinks, never calls a model, never goes on the internet.
> It only runs the exact file / shell / memory operations Mikey asks for,
> inside one workspace folder, with a blocklist and an audit log.

## What this is

A small, auditable Python daemon that runs on Baji's laptop (Arch Linux,
KDE Plasma, Wayland) and gives Mikey a *virtual body* on that laptop:

- Mikey talks to the daemon over HTTP+JSON, through the **private
  Tailscale network** (never the public internet by default).
- The daemon executes: list files, read files, write files, run shell
  commands, and store/search small long-term memories.
- Every request needs a bearer token, stays inside the workspace jail,
  passes a visible command blocklist, and is written to an audit log.

## What this is NOT

- **No model.** No Ollama, no local LLM, no embeddings, no LLM API calls
  from the daemon. Every mention of Ollama in this repo states only
  that it is **not used**.
- **No fallback.** If the daemon is stopped, or Tailscale is down, Mikey
  simply cannot reach the laptop. Nothing else takes over. That is the
  point: Baji stays in control with an obvious kill switch.
- **No screen control.** v0.1 is files + shell only. No GUI automation,
  no Wayland input control, no screenshots, no keystrokes.
- **No telemetry, no auto-start, no public bind.** The daemon binds
  `127.0.0.1` by default, starts only when Baji starts it, and sends
  nothing anywhere on its own.

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
                      +-- audit log: ~/.local/share/hydra/audit.jsonl
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
| `hydra/daemon.py` | HTTP API + exec/read/write/list/shutdown |
| `hydra/security.py` | Workspace jail + command blocklist + token checks |
| `hydra/memory_store.py` | SQLite FTS5 + graph memory (no model) |
| `hydra/audit.py` | Append-only JSONL audit log |
| `hydra_client.py` | Stdlib test client |
| `scripts/generate_token.py` | 0600 token generator |
| `scripts/install-arch.sh` | Arch setup checks + dirs + token |
| `scripts/start-daemon.sh` | Foreground start (manual only) |
| `systemd/hydra.service` | Opt-in user unit (NOT enabled by setup) |
| `config.example.json` | Example config (no secrets in it) |
| `tests/test_daemon.py` | Stdlib unittest suite |
| `docs/ARCHITECTURE.md` | Design + endpoint examples |
| `SECURITY.md` | Threat model + rotation + revocation |
