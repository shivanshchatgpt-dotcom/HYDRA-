# GUI_SETUP.md — HYDRA v0.2 GUI + sandbox setup (Baji's laptop)

The v0.2 file/shell/search/job/memory endpoints need nothing beyond
Python. This page covers only the **optional** parts: screenshots,
mouse/keyboard input, and the bubblewrap sandbox. Everything here is
run by Baji, on the laptop, by hand. The daemon and the install
script never install packages, never use sudo, and never change
system configuration.

Target machine: Arch Linux, KDE Plasma 6.7.4, Wayland, user `baji`.
If a step below does not match what you see, stop and tell Mikey —
do not improvise with sudo.

## 1. The one rule: start the daemon inside the graphical session

`/screen/shot` and `/input/*` only work when the daemon process has
the graphical session's environment:

- `WAYLAND_DISPLAY` set (e.g. `wayland-0`), and
- access to the session D-Bus (`DBUS_SESSION_BUS_ADDRESS`), and
- for ydotool, access to the `ydotoold` socket.

The reliable way: open **Konsole inside the KDE session** and start
the daemon there:

```bash
cd ~/hydra-harness
./scripts/start-daemon.sh --bind <TAILSCALE-IP> --port 8765
```

Check first: `echo $WAYLAND_DISPLAY` should print something like
`wayland-0`. If it is empty, you are not in a graphical session
(e.g. SSH without the session env) and the GUI endpoints will return
501 saying so. The systemd user unit (`systemd/hydra.service`) may
or may not have `WAYLAND_DISPLAY` depending on when it starts; if the
GUI endpoints 501 from the unit, use the Konsole method.

## 2. Screenshots: spectacle (primary), grim, or portal

`/screen/shot` tries, in order:

1. **spectacle** (KDE's screenshot tool, usually already installed
   with Plasma): `spectacle -b -f -o <file>`. No extra permission
   setup — it runs as your user inside your session.
   Install if missing: `sudo pacman -S spectacle`
2. **grim** (generic Wayland screenshot tool): needs permission to
   capture the compositor's buffers. On KDE this generally does NOT
   work (KWin does not implement the `wlr-screencopy` protocol grim
   needs), so grim is a fallback for other compositors, not the
   expected path on Plasma.
   Install if wanted: `sudo pacman -S grim`
3. **xdg-desktop-portal** Screenshot method over D-Bus
   (`org.freedesktop.portal.Desktop`). The first call pops a KDE
   permission dialog asking whether to allow the screenshot; allow
   it. Later calls may or may not re-prompt depending on the portal
   settings. Needs `xdg-desktop-portal` + `xdg-desktop-portal-kde`
   (both normally present on Plasma) and a session bus. This path
   exists so screenshots still work if spectacle is removed.

Verify by hand first:

```bash
spectacle -b -f -o /tmp/shot-test.png && ls -la /tmp/shot-test.png
```

Then through the daemon:

```bash
python3 hydra_client.py screen-shot --save-to shots/shot.png
```

Expected: a real PNG inside `~/hydra-workspace/shots/`. If every
backend fails, the endpoint returns 501 listing each backend's error
— that output is the diagnostic; send it to Mikey.

## 3. Mouse/keyboard input: ydotool + ydotoold + input group

Wayland does not let normal apps fake input. `ydotool` works through
a privileged daemon (`ydotoold`) that writes to `/dev/uinput`.

Setup (one-time, by Baji):

```bash
sudo pacman -S ydotool
sudo systemctl enable --now ydotoold
sudo usermod -aG input baji
```

Then **log out and back in** (group membership only applies to new
sessions). Verify:

```bash
groups            # should list: input
ydotool mousemove --absolute -x 100 -y 100   # mouse should move
```

Notes:

- `ydotoold` runs as root via systemd; `ydotool` talks to it over a
  socket. If `ydotool` says "permission denied" or "cannot connect",
  check `systemctl status ydotoold` and the `input` group first.
- ydotool types with the current keyboard layout. Non-ASCII text may
  not type correctly depending on layout — ASCII is reliable.
- `/input/type` is capped at 4000 chars per call and the text is
  never written to the audit log.

## 4. Sandbox: bubblewrap + user namespaces

Sandboxed exec (`"sandbox": true`) needs bubblewrap:

```bash
sudo pacman -S bubblewrap
```

It also needs **unprivileged user namespaces**, which stock Arch
kernels allow (`kernel.unprivileged_userns_clone` defaults to 1 on
Arch). Verify:

```bash
bwrap --ro-bind / / --tmpfs /tmp --proc /proc --dev /dev \
      --unshare-all --die-with-parent /bin/sh -c 'echo sandbox-ok'
```

Expected output: `sandbox-ok`. If it prints a namespace/permission
error, the kernel or sysctl is restricting user namespaces — tell
Mikey the exact error; do not change kernel settings on your own.

Then through the daemon:

```bash
python3 hydra_client.py exec --command "pwd && touch /etc/should-fail" \
    --sandbox
```

Expected: `pwd` prints the workspace, and the `/etc` write fails
(read-only system). A sandboxed `curl` with default settings should
fail too (no network), unless `--sandbox-allow-network` is used.

## 5. Live verification checklist (do with Mikey, on the laptop)

- [ ] `echo $WAYLAND_DISPLAY` prints a value in the daemon's terminal.
- [ ] `python3 hydra_client.py screen-shot --save-to shots/shot.png`
      produces a real PNG of the current screen; check width/height
      in the response match the display.
- [ ] `python3 hydra_client.py input-move --x 100 --y 100` moves the
      pointer; `input-click` clicks; `input-type --text "test"` types
      into a focused text field (open Kate/Konsole first, watch it).
- [ ] `python3 hydra_client.py exec --command "id; pwd" --sandbox`
      succeeds; `exec --command "touch /tmp/x && ls /tmp/x" --sandbox`
      shows the file exists inside the sandbox's tmpfs only.
- [ ] Without the tools installed (fresh check), each endpoint
      returns 501 naming the missing tool — already covered by the
      automated tests on machines without the tools.
- [ ] Confirm the audit log (`~/.local/share/hydra/audit.jsonl`)
      shows the GUI/job/sandbox calls with no typed text and no
      search patterns.

## 6. Troubleshooting quick reference

| Symptom | Likely cause | Check |
|---|---|---|
| 501 "no graphical session" | Daemon started outside the session | Start from Konsole; `echo $WAYLAND_DISPLAY` |
| 501 naming spectacle/grim/portal | No screenshot backend worked | Run `spectacle -b -f -o /tmp/x.png` by hand; read the error |
| Portal dialog never appears | Portal backend missing | `pacman -Q xdg-desktop-portal-kde` |
| 501 naming ydotool | ydotool not installed | `sudo pacman -S ydotool` |
| ydotool "permission denied" | Not in `input` group / ydotoold down | `groups`, `systemctl status ydotoold`, re-login |
| 501 naming bubblewrap | bwrap not installed | `sudo pacman -S bubblewrap` |
| bwrap namespace error | User namespaces restricted | Run the verify command in §4, send Mikey the error |
| Click/type goes to wrong window | Focus was elsewhere | Click the target window first, or move+click with coordinates |

## 7. What is NOT set up here

Browser control (CDP) arrived in v0.3 and is documented separately:
see `docs/BROWSER_SETUP.md` for the Chrome launch, the
`scripts/chrome-debug.sh` helper, and the live CDP checklist. It
needs no GUI tooling from this document — only Chrome running with
`--remote-debugging-port` on loopback.
