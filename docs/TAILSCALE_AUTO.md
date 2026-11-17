# TAILSCALE_AUTO.md — HYDRA v0.4 Tailscale-auto

Tailscale-auto makes Tailscale setup and daemon reachability
frictionless and self-checking, **without changing the security
model**. The daemon still binds a private address only, still
requires the bearer token on every request, still makes no
outbound calls, and still never stores or handles credentials.
Read `SECURITY.md` first if you have not.

## Honest limits (read these before anything else)

- **The per-session token handoff is NOT solved.** Mikey never
  stores the token, and the secure vault cannot hold a credential
  for a private `100.x` address (it needs a public hostname). Every
  new chat session still needs a fresh token handoff from Baji.
  Tailscale-auto removes the *address* friction (finding and
  re-typing the Tailscale IP), not the token handoff.
- **Laptop off or asleep means nothing works.** There is no relay,
  no wake-on-LAN, no polling, and no cloud queue. If the laptop is
  off, Mikey cannot reach it. That is by design.
- **First approve still needed once per new Muse device.** A new
  device joining Baji's tailnet must still be approved once in the
  Tailscale admin console. Nothing here automates that.
- **No clone-and-instant-connect.** Cloning this repo does not by
  itself connect anything: Tailscale must be installed and logged
  in, the daemon must be running, and the token handoff must have
  happened. Anyone claiming otherwise is wrong.
- **No relay, no Cloudflare, no HTTPS termination, no public bind,
  no credential storage, no auto-update, no outbound call from the
  daemon.** Tailscale-auto adds none of these. The daemon binds
  `127.0.0.1` by default, or a resolved Tailscale IP when asked
  (see below). It never binds `0.0.0.0` or a public address.
- **The doctor never fixes anything.** It reports and prints the
  command Baji runs by hand. The setup script never executes
  `sudo` itself. The kill switch never runs remotely.

## Setup flow (first time, on the laptop)

```bash
cd ~/hydra-harness
./scripts/install-arch.sh          # checks, dirs, token file
./scripts/setup-tailscale.sh       # checks + prints exact commands
```

`setup-tailscale.sh` only **checks** the current state and **prints**
the commands Baji runs by hand (install Tailscale, enable
`tailscaled`, `tailscale up`, approve the device, start the daemon).
It never executes `sudo` itself: every `sudo` in the script is
inside printed instruction text. If free disk space is below
5 GiB it warns and explicitly says **do NOT run a full
`pacman -Syu`** (a full upgrade needs much more free space than a
targeted `pacman -S tailscale`).

One opt-in flag exists:

```bash
./scripts/setup-tailscale.sh --enable-autostart
```

It runs `systemctl --user daemon-reload` and
`systemctl --user enable --now hydra` — user scope only, no `sudo` —
and only when the unit file is already installed at
`~/.config/systemd/user/hydra.service`. Without the unit file it
prints the install steps and exits 1. Without the flag it never
touches systemd at all.

## Starting the daemon with `--bind tailscale`

```bash
./scripts/start-daemon.sh --bind tailscale
```

At startup the daemon resolves the current Tailscale IPv4 by
running the local `tailscale ip -4` command (no `sudo`, local CLI
only, fixed argument list, scrubbed environment) and binds to
that IP. The IP is printed in the startup banner; the token is
never logged.

It **fails closed** with a clear error and a non-zero exit code
when:

- the `tailscale` binary is missing (`install it: sudo pacman -S
  tailscale`), or
- `tailscaled` is down / the device is not logged in (`run: sudo
  tailscale up`), or
- the CLI returns no IPv4 address in `100.64.0.0/10`.

The default is unchanged: without `--bind`, the daemon binds
`127.0.0.1`. The existing bind validation still applies after
resolution (loopback + `100.64.0.0/10` only; `0.0.0.0` and public
addresses are refused). The value `tailscale` also works in the
config file (`"bind": "tailscale"`) and in `HYDRA_BIND`.

The systemd unit (`systemd/hydra.service`) still binds
`127.0.0.1` by default; edit its `ExecStart` to add
`--bind tailscale` only if Baji wants the unit reachable over
Tailscale. Note the unit starts at login, possibly before
`tailscaled` is up: if resolution fails, the daemon exits and
`Restart=always` retries it in 5 seconds — but if Tailscale is
never logged in, it will keep failing, which is the correct
fail-closed behaviour. The doctor (`/system/doctor`) shows
whether the unit is enabled/active.

## Pairing card

```bash
./scripts/pair.sh
```

Read-only. Prints one card: Tailscale IP/state/host, the daemon
URLs, the token file's SHA-256 fingerprint (**first 12 characters
only**), token file mode, workspace, and systemd status, plus the
next steps. It never prints the token value, never runs `sudo` or
`pacman`, and never writes anything.

Example output (values are illustrative):

```text
=== HYDRA pairing card (read-only) ===

Tailscale IP:    100.74.209.52
Tailscale state: Running
Tailscale host:  arch
Daemon URL (Tailscale): http://100.74.209.52:8765
Daemon URL (localhost): http://127.0.0.1:8765

Token file:      /home/baji/.local/share/hydra/token
Token fingerprint (SHA-256, first 12 chars): 9f2c41ab77d0
Token file mode: 600

Workspace:       /home/baji/hydra-workspace
systemd unit:    enabled=enabled, active=active

Next steps: ...
```

The fingerprint lets Baji confirm *which* token file is in use
without ever revealing the token.

## Endpoints

### POST /system/tailscale (auth + audit, read-only)

Runs `tailscale status --json` via a fixed argument list, parses
it, and returns only the safe fields. No node keys, no full peer
list, no peer identities, no tokens — ever. The audit entry
records `connected` + `peer_count` only.

Request: `{}` (empty object; extra fields are ignored).

Connected:

```json
{"ok": true, "connected": true, "backend_state": "Running",
 "self_hostname": "arch", "self_ip": "100.74.209.52",
 "version": "1.104.7", "peer_count": 2}
```

Installed but not logged in (parseable state, not an error):

```json
{"ok": true, "connected": false, "backend_state": "NeedsLogin",
 "self_hostname": "arch", "self_ip": null,
 "version": "1.104.7", "peer_count": 0}
```

- `401` without a valid token (same as every POST endpoint).
- `501` when the `tailscale` binary is missing (error names the
  install command).
- `502` when `tailscaled` is down or its output is unparseable.

### POST /system/doctor (auth + audit, read-only, no auto-fix)

Runs the checks in `hydra/doctor.py` and returns one report. The
doctor never fixes anything, never reads the token file's
contents (it stats the file mode only), and never prints a secret.
The audit entry records `overall` + the pass/warn/fail summary
only.

Request: `{}`.

```json
{"ok": true, "bind": "100.74.209.52", "bind_mode": "tailscale",
 "port": 8765, "workspace": "/home/baji/hydra-workspace",
 "data_dir": "/home/baji/.local/share/hydra",
 "checks": [
   {"name": "tailscale_present", "status": "pass",
    "detail": "tailscale is installed.", "fix_hint": null},
   {"name": "tailscale_connected", "status": "pass",
    "detail": "Tailscale is Running as arch (100.74.209.52), 2 peer(s) visible.",
    "fix_hint": null},
   {"name": "tool_melt", "status": "pass",
    "detail": "melt is installed (/video/render/* endpoints).",
    "fix_hint": null},
   {"name": "disk_free", "status": "warn",
    "detail": "4.7 GiB free on the workspace filesystem (5046586572 bytes free of 164926042112).",
    "fix_hint": "Free disk space before any render or upgrade; do NOT run a full pacman -Syu while space is this low (see docs/TAILSCALE_AUTO.md)."},
   {"name": "token_permissions", "status": "pass",
    "detail": "token file exists with mode 0o600 (contents never read by the doctor).",
    "fix_hint": null},
   {"name": "systemd_unit", "status": "pass",
    "detail": "hydra unit: enabled=enabled, active=active.",
    "fix_hint": null},
   {"name": "bind_mode", "status": "pass",
    "detail": "daemon binds 100.74.209.52:8765 (Tailscale address; reachable from the tailnet with the token).",
    "fix_hint": null}
 ],
 "summary": {"pass": 12, "warn": 1, "fail": 0},
 "overall": "warn"}
```

Checks (each `pass` / `warn` / `fail`, with a `fix_hint` naming
the command Baji runs by hand):

| Check | What it verifies |
|---|---|
| `tailscale_present` | `tailscale` binary on PATH (fail when missing) |
| `tailscale_connected` | `tailscale status --json` parses with `BackendState: Running` and a self IP (fail otherwise) |
| `tool_melt`, `tool_ffmpeg`, `tool_ffprobe`, `tool_kdenlive`, `tool_bwrap`, `tool_rg` | tool presence; warn when missing, with a `sudo pacman -S <package>` hint |
| `disk_free` | free bytes on the workspace filesystem; **fail below 1 GiB, warn below 5 GiB** |
| `token_permissions` | token file exists with mode exactly `0600` (fail otherwise; contents never read) |
| `workspace` | workspace directory exists (fail when missing) |
| `systemd_unit` | read-only `systemctl --user is-enabled` / `is-active` for the `hydra` unit (warn when disabled/inactive/missing) |
| `bind_mode` | bind address is loopback or `100.64.0.0/10` (fail for anything else) |

`overall` is `fail` if any check fails, else `warn` if any warns,
else `pass`. A loopback bind passes the bind check (with a hint
that Mikey cannot reach it over Tailscale) — loopback is safe,
just not remote.

### GET /health gains `tailscale` fields (no auth, minimal)

```json
{"ok": true, "service": "hydra-daemon", "version": "0.4.0",
 "uptime_seconds": 123.4, "jobs_running": 1, "jobs_finished": 3,
 "video_tools": {"kdenlive": true, "melt": true,
                 "ffmpeg": true, "ffprobe": true},
 "tailscale": {"present": true, "connected": true,
               "ip": "100.74.209.52"}}
```

When Tailscale is missing or down the fields degrade honestly to
`{"present": false, "connected": false, "ip": null}` — `/health`
never fails because of Tailscale. No secrets: only presence,
connection state, and the Tailscale IP (which is already visible
to anyone on the tailnet).

## Kill switch script

```bash
./scripts/kill-switch.sh              # stop unit, print what would be deleted, delete NOTHING
./scripts/kill-switch.sh --confirm    # stop unit, then delete token + data dir
```

Local script only — there is **no daemon endpoint** for it, so it
can never be triggered remotely. Without `--confirm` nothing is
deleted. With `--confirm` it prints exactly what will be deleted
first, refuses unsafe data-dir values (`/`, `$HOME`,
`$HOME/.local/share`), stops the HYDRA user unit (best effort),
prints (does not run) the `sudo tailscale down` command for Baji
to run himself, and then deletes the data dir (token + `jobs.db` +
`video.db` + logs + memory + audit log). Detached jobs survive
(see `docs/AUTONOMY.md`); kill them first if that matters.

This is the scripted form of the revocation steps in
`SECURITY.md`; the manual steps there remain the reference.

## Live verification checklist (laptop, NOT yet done)

Everything in this section needs Baji's laptop (on, awake, daemon
running, fresh token handoff) and has **not** been verified on the
build VM, which has no `tailscaled`, no systemd user session, and
no real tailnet. The automated suite proves the logic against
fake `tailscale`/`systemctl` binaries only.

- [ ] `./scripts/setup-tailscale.sh` output matches the real
      laptop state (Tailscale installed, logged in).
- [ ] `./scripts/pair.sh` prints the real Tailscale IP and a
      12-char fingerprint; the token value appears nowhere.
- [ ] `./scripts/start-daemon.sh --bind tailscale` starts, the
      banner shows the Tailscale IP, and `/health` from the VM
      over Tailscale returns `0.4.0` with matching `tailscale` fields.
- [ ] `POST /system/tailscale` returns the real state
      (connected, hostname, peer count) with no keys anywhere in
      the response or the audit log.
- [ ] `POST /system/doctor` returns the real report (disk free,
      tool presence, unit status, bind mode); `overall` matches
      the laptop's real state.
- [ ] With `tailscaled` stopped, the daemon started with
      `--bind tailscale` refuses to start with the clear error
      (fail-closed), and `/system/tailscale` on a loopback-bound
      daemon returns 502.
- [ ] `./scripts/kill-switch.sh` without `--confirm` deletes
      nothing; with `--confirm` on a scratch data dir it deletes
      exactly that dir.
- [ ] The systemd unit with `--bind tailscale` in `ExecStart`
      comes up after a reboot once `tailscaled` is running.

## Future investigation (unverified, never claimed working)

- **MagicDNS**: Tailscale can resolve device hostnames (e.g.
  `arch.<tailnet>.ts.net`) instead of numeric IPs. Whether the
  Muse VM's resolver can use it, and whether binding/resolution
  should prefer a hostname, is uninvestigated. Do not assume it
  works; the numeric-IP path above is the supported one.
- **Tailscale Serve / Funnel**: exposing the daemon through
  Tailscale's own HTTPS serving instead of a raw socket bind.
  Uninvestigated, and it would change the threat model (TLS
  termination, exposure scope) — it needs a security review in
  `SECURITY.md` before any build, and it is not in v0.4.

Both stay on this list until someone verifies them live and
updates this document with evidence. Until then, treat any claim
that they work as unproven.
