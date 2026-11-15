# SECURITY.md — HYDRA v0.1

Read this before running the daemon bound to anything other than
`127.0.0.1`. The daemon is powerful *inside its workspace*: anyone who
can reach it and knows the token can read/write files and run shell
commands there. The whole security model is: **keep the reach small
and the token secret.**

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

**Out of scope for v0.1 (known gaps, stated honestly):**
- No sandbox / container / VM isolation. Commands run as Baji's user,
  with Baji's normal permissions, inside the workspace jail only.
  A command that stays inside the workspace but is still harmful
  (e.g. deleting all workspace files with plain `rm file`) is NOT
  blocked. The jail limits *where*, not *whether* work is destructive
  inside the workspace. Keep backups of anything important.
- HTTP, not HTTPS, on the Tailscale interface. Tailscale's WireGuard
  transport encrypts the traffic; the daemon itself does not do TLS.
  Never bind the daemon to a public interface (see below).
- No per-request user approval queue in v0.1. Approval happens at a
  coarser level: Baji starts/stops the daemon, and Mikey shows Baji
  what he is doing in chat. A future version could add an approval
  prompt per /exec; v0.1 does not have one.
- The audit log is local and append-only *from the daemon*. A local
  attacker with Baji's account could edit it. It is for Baji's review,
  not forensic proof.

## The rules that protect the laptop

1. **Bind:** `127.0.0.1` by default. A Tailscale IP (`100.64.0.0/10`)
   is allowed when explicitly configured. Anything else, including
   `0.0.0.0`, refuses to start unless `--allow-public` is passed, which
   prints a loud warning. **Do not use `--allow-public`.**
2. **Token:** a random 256-bit bearer token, stored 0600 at
   `~/.local/share/hydra/token`. The daemon refuses to start if that
   file is group/other-readable. The token is never logged, never in
   git (see `.gitignore`), never in the config file, and must never be
   pasted into chat or screenshots.
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
5. **Limits:** exec timeout (default 30s, max 300s), output truncation
   (200KB per stream), request body cap (5MB), no TTY, stdin closed,
   child environment scrubbed of `HYDRA_*` variables.
6. **Audit:** every authenticated request is appended to
   `~/.local/share/hydra/audit.jsonl` with timestamp, endpoint,
   redacted summary (lengths/hashes, truncated command preview), and
   outcome. No tokens, no full file contents, no full memory text.

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
2. `sudo tailscale down` (or remove the `muse` device in the Tailscale
   admin console).
3. Delete the token + data: `rm -rf ~/.local/share/hydra`
4. Delete the code if desired: `rm -rf ~/hydra-harness`

After step 1 alone, Mikey already has no access. Steps 2-4 remove the
credentials and network path as well.

## Reporting a problem

This is Baji's personal harness, not a published service. If Mikey
notices a bypass in the jail or blocklist, he must tell Baji, stop
using the affected endpoint, and fix the code before continuing. Do
not work around a security refusal by asking Baji to weaken the
daemon; run the command in Baji's own terminal instead.
