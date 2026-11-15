# BROWSER_SETUP.md — CDP browser control (HYDRA v0.3)

HYDRA v0.3 can drive Baji's own Chrome through the Chrome DevTools
Protocol (CDP): navigate, evaluate JavaScript, read page content,
take page screenshots, click, and type. The daemon talks to Chrome
only on loopback (`127.0.0.1:9222` by default) with a hand-rolled
stdlib WebSocket client — no new dependencies.

## What this means, honestly

CDP access to Chrome is equivalent to sitting at the keyboard inside
that browser. Chrome runs with Baji's **normal profile**, so whatever
he is logged into is reachable through these endpoints. That is the
point (Mikey can do browser work as Baji), and it is also the reason
for the hard limits:

- **Loopback only.** The daemon refuses any non-loopback CDP target
  (403, before a socket opens). Nobody off this laptop can point the
  daemon at their browser, and the daemon can never be pointed at a
  remote browser.
- **Navigation allowlist.** Only `http:`, `https:`, and `about:blank`.
  `file:` URLs are refused, so the browser cannot be used to read
  local files around the workspace jail.
- **No credential handling.** The daemon never reads browser profiles,
  cookies, or keychains. It only drives the browser Baji himself
  started. If Baji is not logged into a site, the daemon is not
  either.
- **Audit redaction.** Audit entries carry lengths/hashes only —
  never URLs, expressions, page content, or typed text.

Only start Chrome in debug mode when browser work is actually needed.
Close it normally afterwards; nothing about the debug flag persists
(the next normal Chrome launch is an ordinary launch).

## Setup (on the laptop, run by Baji)

### 1. Install Chrome (if not already installed)

Any of these works: `google-chrome`, `google-chrome-stable`,
`chromium`, `chromium-browser`. On Arch, Chrome is in the AUR
(`google-chrome`); Chromium is in the repos (`sudo pacman -S
chromium`). The install script (`scripts/install-arch.sh`) reports
whether one was found — it never installs anything itself.

### 2. Close Chrome completely (only if it is already running)

This step matters: **if Chrome is already running WITHOUT the debug
flag, launching a second copy WITH the flag does not enable
debugging** — the new process just hands off to the running instance
and exits. If Chrome is running, close ALL its windows first
(including any background/tray instance), then continue.

### 3. Launch Chrome with remote debugging

From the repo directory:

```bash
cd ~/hydra-harness
chmod +x scripts/*.sh   # once, after cloning (snapshot pushes don't keep modes)
./scripts/chrome-debug.sh
```

That script:

- checks whether something is already listening on `127.0.0.1:9222`
  and exits quietly if so (no second instance is started);
- otherwise launches Chrome once with
  `--remote-debugging-port=9222`, using Baji's **normal profile**;
- **never** kills, restarts, or touches a running Chrome.

A custom port works too: `./scripts/chrome-debug.sh 9333` — then
pass `"port": 9333` in `/browser/*` calls (or `--port 9333` with
`hydra_client.py`). Extra arguments after the port are passed through
to Chrome unchanged.

The exact equivalent manual command is:

```bash
google-chrome --remote-debugging-port=9222
```

(run with Baji's normal user, normal profile, no other flags needed).
The script is just a safer wrapper around this one line.

### 4. Verify

```bash
# Daemon side (daemon must be running):
python3 hydra_client.py browser-status
# -> {"available": true, "browser": "Chrome/...", "target_count": 1, ...}

# Or directly, without the daemon:
curl -s http://127.0.0.1:9222/json/version
```

If `browser-status` says `available: false`, Chrome is not in debug
mode — go back to step 2 (the usual cause is a Chrome that was
already running without the flag).

## Using it

```bash
python3 hydra_client.py browser-navigate --url https://example.com
python3 hydra_client.py browser-evaluate --expression "document.title"
python3 hydra_client.py browser-content
python3 hydra_client.py browser-shot --save-to shots/page.png
python3 hydra_client.py browser-click --x 100 --y 200
python3 hydra_client.py browser-type --text "hello"
```

Endpoint reference with request/response shapes: see
`docs/ARCHITECTURE.md`. Notes:

- **Target selection:** by default the first open page tab is used.
  Pass `--target-id <id>` (from `browser-status`) to pick another.
  If Chrome has no open tab, the endpoints return 502 naming the gap
  — open a tab first.
- **Click coordinates** are CSS viewport coordinates in the target
  page (like CDP's `Input.dispatchMouseEvent`), not screen pixels.
- **Type** inserts text at the current focus (like CDP's
  `Input.insertText`); click the field first if it is not focused.
- **Evaluate** returns the value JSON-serialised (200KB cap); a page
  exception comes back as `{"value": null, "exception": "..."}`.
- **Content** returns title + visible text + HTML, each capped at
  100KB with `*_truncated` flags.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `available: false` / connection refused on 9222 | Chrome not running with the flag | Close ALL Chrome windows, then run `./scripts/chrome-debug.sh` |
| Script says "already up" but `browser-status` fails | Something else on port 9222 | Check with `curl -s http://127.0.0.1:9222/json/version`; if it is not Chrome, use another port: `./scripts/chrome-debug.sh 9333` |
| `no page target` (502) | Chrome open but no tab | Open any tab in the debug Chrome |
| `unknown target id` | Tab closed / wrong id | Re-run `browser-status` and use a current id |
| Click/type seems to do nothing | Wrong tab, or page not focused | Check `browser-status` URL/title; click the field first, then type |
| Chrome shows a "remote debugging" infobar | Normal | It is Chrome telling you debugging is on. It disappears when Chrome is closed and relaunched normally. |

## Stopping

Close Chrome normally (all windows). The debug port closes with it.
The daemon's `/browser/*` endpoints then return 501 until Chrome is
launched in debug mode again. Nothing persists: no config change, no
service, no profile change.

## Live verification checklist (on Baji's laptop)

Not verified on the test VM (no Chrome there). The automated suite
proves the protocol against an independent fake CDP server; these
steps prove it against real Chrome:

- [ ] `./scripts/chrome-debug.sh` starts Chrome; `browser-status`
      reports `available: true` with the real Chrome version.
- [ ] `browser-navigate` to a real page; the page visibly loads in
      the Chrome window.
- [ ] `browser-evaluate` with `document.title` returns the page title.
- [ ] `browser-content` returns the page's visible text.
- [ ] `browser-shot --save-to` writes a real PNG of the page.
- [ ] `browser-click` + `browser-type` interact with a real form field.
- [ ] Close Chrome; `browser-status` reports `available: false`.
- [ ] Relaunch Chrome normally (no flag); `browser-status` still
      reports `available: false` (proves the flag does not persist).
