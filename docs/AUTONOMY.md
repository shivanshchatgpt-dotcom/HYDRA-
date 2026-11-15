# AUTONOMY.md — What runs without Baji, what still needs him (HYDRA v0.3)

Baji's direction for v0.3: like OpenCode and Hermes Agent, HYDRA
should do all work WITHOUT him present. This document states exactly
what v0.3 delivers on that, what it deliberately does not, and what
still needs Baji. No overpromising: every item below names the
mechanism that makes it true.

## What runs without Baji present

| Capability | Mechanism | Since |
|---|---|---|
| File work (read/write/edit/search) | Daemon + token + Tailscale; Mikey drives every call | v0.1/v0.2 |
| Shell commands (sync) | `/exec`, same jail + blocklist | v0.1 |
| Background jobs with output polling | `/jobs/*`, 32-job cap, deadlines | v0.2 |
| Jobs surviving a daemon restart | `jobs.db` + log files; re-attach by PID liveness + starttime check | v0.3 |
| Jobs surviving daemon shutdown/crash | `"detached": true`: own session, file output, skipped by kill-all | v0.3 |
| Daemon coming back after a crash | Opt-in systemd unit: `Restart=always`, `RestartSec=5` | v0.3 |
| Job records + output queryable after restart | `jobs.db` persists; `/jobs/poll` reads log files | v0.3 |
| Read-only system/git checks | `/system/info`, `/git/status` | v0.3 |
| Browser work in Baji's Chrome | CDP over loopback, once Chrome runs with `--remote-debugging-port` | v0.3 |
| Long-term memory (keyword + graph) | SQLite FTS5 + relations, no model | v0.1 |
| Detached app launches (taskbar-visible) | `setsid <app> &` via `/exec` or a detached job | v0.1 (verified live) |

In short: once the daemon is running and reachable, Mikey can do a
full work session — code, builds, git, tests, ffmpeg renders, browser
checks — with Baji nowhere near the keyboard, and long jobs keep
going across daemon restarts. That is the OpenCode/Hermes-style
"works without me" behaviour, for the executor side.

## What still needs Baji

These are hard requirements, not inconveniences. Nothing in v0.3
removes them, and no future version of this design can remove the
first three without changing what HYDRA is.

1. **The laptop must be on and awake.** A suspended, sleeping, or
   powered-off laptop runs nothing and answers nothing. HYDRA has no
   wake-on-LAN, no cloud relay, and no way to wake the machine.
   (Hermes Agent has the same limit for its local backend; its
   cloud backends — Modal, Daytona, Vercel Sandbox — are a different
   product with a different trust model, and Baji ruled out anything
   that is not his own laptop.)
2. **The daemon must be running.** Either Baji starts it
   (`./scripts/start-daemon.sh`), or he enables the opt-in systemd
   unit once (`systemctl --user enable --now hydra`) so it starts on
   login and restarts after crashes. Setup never enables it for him.
   If the daemon is stopped and the unit is not enabled, Mikey has
   no access at all — by design (the kill switch).
3. **A token handoff per new chat session.** The bearer token is
   never stored in memory or files on Mikey's side. Each new chat
   session needs Baji to hand it over again through the approved
   route (transient, session-scoped). Without it, only `/health`
   answers. This is the price of the token never being persisted
   anywhere outside the laptop.
4. **Tailscale must be up** for Mikey (cloud) to reach the laptop.
   If Tailscale is down, only localhost access works (Baji's own
   terminal). The daemon cannot bring Tailscale up itself (that
   needs `sudo`, which the blocklist refuses).
5. **Chrome must be in debug mode for browser work.** Baji starts
   Chrome with `--remote-debugging-port=9222` (via
   `scripts/chrome-debug.sh`) when browser work is needed. The
   daemon never starts Chrome itself, and a Chrome already running
   without the flag must be closed first. See `docs/BROWSER_SETUP.md`.
6. **First-time GUI permissions** (only if GUI endpoints are used):
   the xdg-desktop-portal screenshot path may show a permission
   dialog the first time; `ydotoold` needs to be running and Baji in
   the `input` group for `/input/*`. See `docs/GUI_SETUP.md`.
7. **Disk space.** The root partition was nearly full on 2026-11-15
   (the full `pacman -Syu` failed for lack of space). Job logs are
   capped (1MB/stream) and pruned with jobs, but renders, builds,
   and downloads still need room. `/system/info` reports free disk
   so Mikey can check before heavy work.
8. **Decisions and approvals stay with Baji.** HYDRA has no approval
   queue and no agent loop: Mikey proposes and acts per request in
   chat, and Baji approves pushes, posts, payments, and anything
   irreversible. The daemon never acts without a request from Mikey,
   and Mikey never acts without Baji's direction for anything
   outward-facing.

## What v0.3 deliberately does NOT add

- **No agent loop on the laptop.** OpenCode and Hermes Agent both
  run an agent loop (the model driving tool calls in a loop). In
  HYDRA the loop lives in the cloud (Mikey). The laptop side stays a
  dumb executor. This is Baji's explicit constraint, not a missing
  feature.
- **No scheduling / cron inside the daemon.** Hermes Agent's gateway
  has a cron scheduler that wakes automations. HYDRA has none: no
  work ever starts on the laptop without a live request. If Baji
  wants scheduled work, it must be requested by Mikey (or by a
  plain systemd timer Baji installs himself, outside HYDRA).
- **No subagents, no sessions, no LSP.** OpenCode's session store
  and subagent model were studied; HYDRA copies only the persistence
  idea (durable job records), not the agent machinery.
- **No model, no embeddings, no telemetry, no HTTPS, no approval
  queue, no credential extraction.** Unchanged since v0.1. See
  `SECURITY.md`.

## Comparison: v0.3 vs OpenCode vs Hermes Agent

| Aspect | HYDRA v0.3 | OpenCode | Hermes Agent |
|---|---|---|---|
| Where the brain runs | Cloud (Mikey) | Local agent loop (model remote) | Gateway process + model |
| Where execution runs | Baji's laptop daemon | Local (or remote via SSH) | Local / Docker / SSH / cloud sandboxes |
| Work survives restart | Yes: jobs.db + logs + re-attach | Yes: session store + message log | Yes: gateway + persistent containers |
| Background tasks | Persistent + detached jobs, poll/kill/list | Background subagents/tasks | `terminal(background=true)` + process tool |
| Sandbox | Opt-in bubblewrap per call | None by default | Container backends (non-local) |
| Browser control | CDP, loopback only, own Chrome | HTTP fetch only (no browser) | CDP browser tools + vault |
| GUI/screen control | Best-effort Wayland endpoints | None | Optional computer-use tools |
| Scheduling | None (deliberate) | None built-in | Cron scheduler in gateway |
| Agent loop on device | None (deliberate) | Yes | Yes |
| Model on device | None (deliberate) | No (remote inference) | No (remote inference) |
| Memory | SQLite FTS + graph, keyword only | Session store + context files | SQLite FTS + LLM summarization |
| Dependencies | Python stdlib only | Bun/Go binaries | Python package + services |
| Kill switch | Stop daemon / Tailscale down | Stop process | Stop gateway |

HYDRA is the only one of the three that is a stdlib-only dumb
executor with a workspace jail, an audit log, Wayland-native
screen/input, and loopback CDP — and the only one with no agent loop
at all. That is the design Baji asked for: all the thinking stays
with Mikey in the cloud; the laptop is only the body.

## Live verification checklist (on Baji's laptop)

- [ ] **Detached jobs across a real restart:** start a detached job
      (`sleep 60` or a real build), Ctrl-C the daemon, restart it,
      `/jobs/poll` shows the job `running` with `recovered: true`.
- [ ] **Autostart unit:** install the unit (opt-in commands in
      `scripts/install-arch.sh` output), `kill` the daemon process,
      confirm systemd restarts it within ~5s (`journalctl --user -u
      hydra`), and confirm `systemctl --user stop hydra` stops it
      for good (no restart).
- [ ] **PID file:** `~/.local/share/hydra/hydra.pid` matches the
      running daemon PID; gone after a clean stop.
- [ ] **CDP against real Chrome:** see `docs/BROWSER_SETUP.md`
      checklist (navigate, evaluate, content, shot, click, type).
- [ ] **`/system/info` + `/git/status`:** sane values on the laptop
      (disk free, tools present, repo status in `~/hydra-harness`).
- [ ] **Crash case:** `kill -9` the daemon mid-job; restart; the
      detached job is re-attached and the finished job's output is
      still pollable.

## References

- OpenCode: `https://github.com/anomalyco/opencode` (MIT)
- Hermes Agent: `https://github.com/NousResearch/hermes-agent` (MIT)
- Hermes Agent tools docs:
  `https://hermes-agent.nousresearch.com/docs/user-guide/features/tools`
- Hermes Agent tools reference:
  `https://hermes-agent.nousresearch.com/docs/reference/tools-reference`
- Chrome DevTools Protocol:
  `https://chromedevtools.github.io/devtools-protocol/`
