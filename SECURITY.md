# SECURITY.md — HYDRA v0.4

Read this before running the daemon bound to anything other than
`127.0.0.1`. The daemon is powerful *inside its workspace*: anyone who
can reach it and knows the token can read/write files and run shell
commands there. In v0.2 the GUI endpoints additionally expose the
whole screen and the keyboard/mouse; in v0.3 the browser endpoints
expose Baji's own logged-in Chrome and the persistent/detached jobs
keep running after the daemon stops; in v0.4 the video endpoints run
CPU-heavy `melt`/`ffmpeg` renders that write large files inside the
workspace, and the Tailscale-auto additions resolve the bind address
from the local `tailscale` CLI and report Tailscale/doctor state
through read-only endpoints — see "GUI endpoints", "Browser
endpoints (CDP)", "Persistent jobs", "Video renders" and rule 12
("Tailscale-auto") below. The whole security
model is: **keep the reach small and the token secret.**

## Threat model

**Trusted:**
- Baji, on his own laptop, running the daemon himself.
- Mikey (cloud assistant), when Baji has asked him to do laptop work
  and the daemon is running.

**Untrusted / assumed hostile:**
- Anyone else on the internet. (The daemon must never be reachable
  from the internet.)
- Other devices on the same Tailscale network that Baji did not intend
  to grant access. (Tailscale ACLs are Baji's control for this; the
  daemon additionally requires the bearer token.)
- Malicious content inside the workspace (e.g. a script Baji did not
  write). The blocklist refuses the most dangerous patterns, but it is
  a safety net, not a sandbox. Do not ask Mikey to run untrusted code.

**Out of scope / known gaps (stated honestly):**
- No sandbox / container / VM isolation **by default**. Commands run
  as Baji's user, with Baji's normal permissions, inside the workspace
  jail only. v0.2 adds an *opt-in* bubblewrap sandbox (`"sandbox":
  true` on `/exec` and `/jobs/start`): system read-only, workspace
  read-write, no network by default. The sandbox is only as strong as
  bubblewrap + unprivileged user namespaces on the host kernel, and it
  does not limit CPU/RAM. A command that stays inside the workspace but
  is still harmful (e.g. deleting all workspace files with plain
  `rm file`) is NOT blocked, sandboxed or not. The jail limits *where*,
  not *whether* work is destructive inside the workspace. Keep backups
  of anything important.
- The GUI endpoints (v0.2) are the widest access the daemon has: a
  screenshot sees the whole screen (all apps, all windows), and input
  control can click/type anywhere, including outside the workspace
  concept entirely. They exist because Baji asked for full open control
  of the laptop, and they are gated only by the bearer token + the
  daemon running. Treat a running daemon with GUI tools installed as
  equivalent to sitting at the keyboard. The audit log records that a
  screenshot/input happened (never typed text), and screenshots are
  returned to the authenticated caller only — but nothing stops the
  caller from seeing private content on screen. Only run the daemon
  with GUI tools installed when Baji wants that level of access.
- The browser endpoints (v0.3) drive Baji's OWN Chrome, with his
  normal profile — including whatever he is logged into. CDP access
  to a browser is equivalent to sitting at the keyboard inside that
  browser: it can read page content and act as the logged-in user.
  The daemon restricts this hard: loopback only (a non-loopback CDP
  target is refused before any socket opens), navigation limited to
  http/https + `about:blank` (`file:` is refused so the browser
  cannot read local files around the jail), and audit entries carry
  lengths/hashes only. But within those limits, treat a daemon with
  Chrome in debug mode as full browser access. Only start Chrome
  with `--remote-debugging-port` when browser work is actually
  needed, and close it normally afterwards. See
  `docs/BROWSER_SETUP.md`.
- Persistent + detached jobs (v0.3): job records and output live in
  `~/.local/share/hydra/jobs.db` and `job-logs/` — command output,
  which may contain sensitive text, persists on disk until the job
  is pruned (32-job cap) or the data dir is deleted. Detached jobs
  are normal processes in their own session: they keep running after
  the daemon stops, so the daemon kill switch stops Mikey's *access*
  instantly but does not stop already-running detached work. Kill
  detached jobs explicitly (`/jobs/kill`, or `ps`/`kill`) when that
  matters. The opt-in systemd unit's `Restart=always` restarts a
  *crashed* daemon in 5 seconds; an explicit
  `systemctl --user stop hydra` still stops it for good.
- Video renders (v0.4): `melt`/`ffmpeg` subprocesses run with fixed
  argument lists (no shell, no caller-supplied flags — project data
  cannot inject render options), read only workspace-jailed media,
  and write only inside the workspace. Renders are CPU-heavy and
  write large files: the single-render lock caps concurrency at one,
  the disk guard refuses renders that do not fit, and the render
  timeout (default 4h, max 24h) bounds any single run. There are no
  CPU/RAM limits beyond that — a render will use the machine hard
  while it runs. Workflow state (`video.db`) and render outputs
  persist on disk until deleted. On daemon restart, recovery kills
  a still-living recorded melt process (SIGKILL to its process
  group) and marks the workflow interrupted/resumable; this is the
  only case where the daemon kills a process it did not spawn in
  this process lifetime, and it only ever targets a melt pid from
  its own workflow table. The daemon never downloads media: manifest
  URLs are provenance notes only, and copyrighted movie/game clips
  must never be fetched through the daemon.
- HTTP, not HTTPS, on the Tailscale interface. Tailscale's WireGuard
  transport encrypts the traffic; the daemon itself does not do TLS.
  Never bind the daemon to a public interface (see below).
- No per-request user approval queue in v0.3. Approval happens at a
  coarser level: Baji starts/stops the daemon, and Mikey shows Baji
  what he is doing in chat. A future version could add an approval
  prompt per /exec; v0.3 does not have one.
- The audit log is local and append-only *from the daemon*. A local
  attacker with Baji's account could edit it. It is for Baji's review,
  not forensic proof.

## The rules that protect the laptop

1. **Bind:** `127.0.0.1` by default. A Tailscale IP (`100.64.0.0/10`)
   is allowed when explicitly configured. The bind value
   `tailscale` (CLI `--bind tailscale`, config `"bind":
   "tailscale"`, or `HYDRA_BIND=tailscale`) resolves the current
   Tailscale IPv4 via the local `tailscale ip -4` command at
   startup and fails closed — the daemon refuses to start — if
   Tailscale is down or returns no CGNAT address. The resolved IP
   still passes the same validation as an explicit one. Anything
   else, including `0.0.0.0`, refuses to start unless
   `--allow-public` is passed, which prints a loud warning. **Do
   not use `--allow-public`.** HTTP only, on the private network:
   Tailscale's WireGuard transport encrypts the traffic; the
   daemon itself does not do TLS and never binds a public
   interface. There is no relay, no Cloudflare tunnel, no polling,
   and no outbound call from the daemon.
2. **Token:** a random 256-bit bearer token, stored 0600 at
   `~/.local/share/hydra/token`. The daemon refuses to start if that
   file is group/other-readable. The token is never logged, never in
   git (see `.gitignore`), never in the config file, and must never be
   pasted into chat or screenshots. The only place the token file is
   ever referenced outside the daemon is `scripts/pair.sh`, which
   prints a SHA-256 fingerprint of the file — first 12 characters
   only — so Baji can confirm which token file is in use without
   revealing the token. The doctor (`/system/doctor`) checks the
   file's mode with `stat` only and never reads its contents. The
   daemon handles no other credential of any kind: no passwords,
   no browser profiles, no keychains, no session stores. There is
   no auto-update and no self-modification anywhere in the daemon.
3. **Workspace jail:** file ops and exec cwd resolve symlinks and must
   stay inside `~/hydra-workspace`. `..` escapes, absolute paths
   outside, and escaping symlinks are refused. `~`, `/etc`, `~/.ssh`,
   browser profiles, and the token/data dir are unreachable through
   the daemon by default. An optional read-only extra path can be
   configured by Baji, for reads only, and defaults to none.
4. **Blocklist:** `sudo`, `su`, `doas`, `pkexec`, recursive `rm`,
   `rm -rf /`, `mkfs`, `dd`, `shutdown`/`reboot`, `chmod 777`,
   `chown`, firewall tools, `ssh`/`scp`, `curl|sh` / `wget|sh`,
   command substitution `$()`/backticks, `.ssh` paths, and any path
   outside the workspace are refused. The full list is in
   `hydra/security.py` — read it, it is meant to be audited.
   `systemctl` management verbs need explicit opt-in config.
5. **Limits:** exec timeout (default 30s, max 300s), job timeout
   (default 1h, max 24h, max 32 concurrent jobs), output truncation
   (200KB per stream), job log files hard-capped at 1MB per stream
   (RLIMIT_FSIZE), request body cap (5MB), search caps (500
   results, 30s), screenshot PNG returned base64 or saved inside the
   workspace only, typed text capped at 4000 chars, browser evaluate
   capped at 200KB / page content at 100KB with `truncated` flags,
   git status capped at 200 entries, video timeline caps (8 tracks,
   200 clips/track, 500 clips total, 500-char titles, 10 preview
   frames), one melt render at a time, render timeout (default 4h,
   max 24h), disk guard refusing renders that do not fit, no TTY,
   stdin closed, child environment scrubbed of `HYDRA_*` variables.
6. **Audit:** every authenticated request is appended to
   `~/.local/share/hydra/audit.jsonl` with timestamp, endpoint,
   redacted summary (lengths/hashes, truncated command preview), and
   outcome. No tokens, no full file contents, no full memory text, no
   search patterns, no typed text, no URLs, no page content. Since
   v0.2 the audit entry is written *before* the response is sent, so
   a returned result is always already logged.
7. **Sandbox (opt-in):** `"sandbox": true` wraps the command in
   bubblewrap: `/` read-only, workspace read-write, fresh `/tmp`,
   own PID/UTS/IPC/cgroup namespaces, no network unless
   `"sandbox_no_network": false`. If `bwrap` is missing the request
   fails with 501 naming bubblewrap — it is never silently run
   unsandboxed. Sandboxed commands still run as Baji's user and can
   still destroy workspace files; the sandbox limits reach outside
   the workspace, not destructiveness inside it.
8. **GUI endpoints (opt-in by tooling):** `/screen/shot` and
   `/input/*` only work when the helper tools are installed
   (spectacle/grim/portal for screenshots, ydotool + ydotoold for
   input) and the daemon runs inside the graphical session. Missing
   tools or a missing session produce a 501 naming the gap — success
   is never faked. Screenshots verify PNG magic bytes before
   returning. See `docs/GUI_SETUP.md` for the exact setup and the
   permissions each tool needs.
9. **Browser endpoints (opt-in by Chrome state, loopback only):**
   `/browser/*` only talks to a Chrome DevTools target on
   `127.0.0.1`/`::1`/`localhost` (default port 9222). Any other host
   is refused with 403 before a socket opens, and the CDP WebSocket
   URL from Chrome is re-validated the same way. Navigation allows
   http/https + `about:blank` only. Chrome must be started by Baji
   with `--remote-debugging-port` (see `scripts/chrome-debug.sh`,
   which never kills an existing Chrome); the daemon never starts
   Chrome itself. Missing Chrome returns 501 naming the launch
   script. CDP protocol failures return 502. See
   `docs/BROWSER_SETUP.md`.
10. **Persistent jobs + resilience:** job records (`jobs.db`) and
    log files live under the data dir (`~/.local/share/hydra`,
    mode 0700) and are never synced anywhere. Re-attachment after a
    restart uses PID liveness plus `/proc/<pid>/stat` starttime, so
    a reused PID cannot be mistaken for the original job. The PID
    file (`hydra.pid`) is removed on clean exit; a stale PID file
    after a crash is harmless (the next start overwrites it — never
    trust it without checking `/health`). The systemd unit's
    `Restart=always` only restarts after unexpected exits; Baji's
    explicit stop always wins.
11. **Video endpoints (v0.4, headless only):** `/video/*` never
    touches the GUI: timelines are MLT XML generated by code and
    rendered by `melt` with fixed consumer arguments. All media,
    project, output, and preview paths are workspace-jailed
    (escapes -> 403); timelines are validated before any file is
    written (no overlapping clips on a track, times inside probed
    media). Missing tools return 501 naming the Arch package; the
    disk guard returns 507. Workflows checkpoint in `video.db`
    (same data dir, mode 0700) and resume only on an explicit
    request — never automatically. Preview responses return
    workspace file paths, never image bytes. See
    `docs/VIDEO_SETUP.md`.
12. **Tailscale-auto (v0.4, read-only + local scripts):**
    `/system/tailscale` and `/system/doctor` are authenticated,
    audit-logged, and read-only: they run fixed local commands
    (`tailscale status --json`, `systemctl --user is-enabled /
    is-active`, `shutil.which`, `stat`) with scrubbed
    environments and return summaries only. `/system/tailscale`
    returns connected state, self hostname/IP, backend state,
    version, and a peer *count* — never node keys, never the peer
    list, never peer identities; its audit entry carries
    `connected` + `peer_count` only. `/system/doctor` never reads
    the token file's contents, never fixes anything, and only
    prints fix *hints* (commands Baji runs by hand); its audit
    entry carries `overall` + the summary only. `GET /health`
    gains `tailscale` presence/connection/IP fields and never
    fails because of Tailscale. The laptop scripts follow the same
    discipline: `scripts/pair.sh` is read-only and prints only a
    12-character token fingerprint; `scripts/setup-tailscale.sh`
    never executes `sudo` (every `sudo` is inside printed
    instruction text) and its opt-in `--enable-autostart` only
    runs user-scope `systemctl --user` commands when the unit file
    is already installed; `scripts/kill-switch.sh` is local only
    (no daemon endpoint, never remote-triggerable) and deletes
    token + data only with an explicit `--confirm` after printing
    exactly what will be deleted. Tailscale-auto does NOT remove
    the per-session token handoff, does NOT add a relay,
    Cloudflare, polling, HTTPS, or a public bind, and does NOT
    change any rule above. See `docs/TAILSCALE_AUTO.md`.

## If the token leaks

Assume anyone with the token + network reach can run workspace commands
until you rotate. Act in this order:

1. Stop the daemon (Ctrl-C / `systemctl --user stop hydra`).
2. Rotate the token: `python3 scripts/generate_token.py --force`
   (this overwrites the token file; the old token stops working the
   moment the daemon restarts with the new file).
3. If you suspect wider compromise, also run `sudo tailscale down`,
   review `~/.local/share/hydra/audit.jsonl` for unfamiliar requests,
   and review the workspace for unfamiliar files.
4. Only restart the daemon when you are satisfied, with the new token.

## Revocation (full removal of access)

1. Stop the daemon and (if installed) disable the unit:
   `systemctl --user disable --now hydra`
2. Kill any detached jobs still running (they survive daemon stop):
   `python3 hydra_client.py jobs-list` before stopping, then
   `jobs-kill` each one — or find them with `ps` and `kill` them.
3. `sudo tailscale down` (or remove the `muse` device in the Tailscale
   admin console).
4. Delete the token + data: `rm -rf ~/.local/share/hydra`
   (this also deletes `jobs.db`, `video.db`, job logs, memory, and
   the audit log). The scripted form is
   `./scripts/kill-switch.sh --confirm` (local only; without
   `--confirm` it deletes nothing).
5. Delete the code if desired: `rm -rf ~/hydra-harness`

After step 1 alone, Mikey already has no access. Steps 2-5 remove the
running work, credentials, and network path as well.

## Reporting a problem

This is Baji's personal harness, not a published service. If Mikey
notices a bypass in the jail or blocklist, he must tell Baji, stop
using the affected endpoint, and fix the code before continuing. Do
not work around a security refusal by asking Baji to weaken the
daemon; run the command in Baji's own terminal instead.
