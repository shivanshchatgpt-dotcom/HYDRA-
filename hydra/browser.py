"""Chrome DevTools Protocol (CDP) browser control, loopback only.

HYDRA v0.3 can drive Baji's own Chrome through the DevTools
Protocol, the same way Hermes Agent's ``browser_exec``/``browser_cdp``
tools drive Chromium. Chrome must be running with remote debugging
enabled on loopback (see docs/BROWSER_SETUP.md and
scripts/chrome-debug.sh). No other browser process is touched, and
no existing Chrome is ever killed.

Hard limits, by design:

* LOOPBACK ONLY. The CDP target must be 127.0.0.1, ::1, or the
  literal name ``localhost``. Any other host is refused before any
  socket is opened. There is no way to point this at a remote
  browser.
* URL allowlist for navigation: ``http:``, ``https:`` and
  ``about:blank`` only. ``file:`` URLs are refused: Chrome can read
  local files, and the browser must not become a way around the
  workspace jail.
* Result caps: evaluate results and page content are capped in
  bytes (200KB / 100KB); the caller gets a ``truncated`` flag,
  never a silent cut.
* The daemon never logs page content or typed text. Audit entries
  carry lengths and hashes only (the daemon layer enforces this;
  this module returns data, it does not log).

CDP reference: https://chromedevtools.github.io/devtools-protocol/
(the protocol Hermes Agent's browser tools also speak). Only the
handful of domains below are used: Target (via /json/list),
Page (navigate, captureScreenshot), Runtime (evaluate) and Input
(dispatchMouseEvent, insertText).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

from .websocket import WebSocketClient, WebSocketClosed, WebSocketError

DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222
CONNECT_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
MAX_URL_CHARS = 8192
MAX_EXPRESSION_CHARS = 16384
MAX_EVAL_BYTES = 200 * 1024
MAX_CONTENT_BYTES = 100 * 1024
MAX_TYPE_CHARS = 4000
MAX_CONTENT_JS_CHARS = 100 * 1024  # slice inside the page first

_ALLOWED_SCHEMES = {"http", "https"}


class CdpError(RuntimeError):
    """A CDP command failed (protocol error, bad target, timeout)."""


class BrowserUnavailableError(CdpError):
    """Chrome with remote debugging is not reachable on loopback."""


# ---------------------------------------------------------------- loopback

def validate_loopback(host: str) -> str:
    """Return the host if it is loopback, else raise ValueError.

    Accepts the literal name ``localhost`` and loopback IP literals
    (127.0.0.0/8, ::1). Everything else — including private LAN and
    Tailscale addresses — is refused: the daemon must only ever
    drive a browser on its own machine.
    """
    if host == "localhost":
        return host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            f"CDP target host {host!r} is not a loopback address") from exc
    if not addr.is_loopback:
        raise ValueError(
            f"refusing non-loopback CDP target {host!r}: "
            "browser control is localhost-only")
    return host


# -------------------------------------------------------------- HTTP probe

def _http_get_json(host: str, port: int, path: str,
                   timeout: float = CONNECT_TIMEOUT) -> object:
    url = f"http://{host}:{port}{path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise BrowserUnavailableError(
            "Chrome remote debugging is not reachable at "
            f"{host}:{port} — start Chrome with "
            "`scripts/chrome-debug.sh` (or "
            "`google-chrome --remote-debugging-port=9222`) and retry. "
            f"Detail: {exc}") from exc


def get_version(host: str = DEFAULT_CDP_HOST,
                port: int = DEFAULT_CDP_PORT) -> dict:
    validate_loopback(host)
    data = _http_get_json(host, port, "/json/version")
    if not isinstance(data, dict):
        raise CdpError("/json/version returned unexpected data")
    return data


def list_targets(host: str = DEFAULT_CDP_HOST,
                 port: int = DEFAULT_CDP_PORT) -> list[dict]:
    validate_loopback(host)
    data = _http_get_json(host, port, "/json/list")
    if not isinstance(data, list):
        raise CdpError("/json/list returned unexpected data")
    return [t for t in data if isinstance(t, dict)]


def browser_status(host: str = DEFAULT_CDP_HOST,
                   port: int = DEFAULT_CDP_PORT) -> dict:
    """Probe Chrome. Never raises for an unreachable browser: it
    returns ``available: false`` with the reason. A non-loopback
    host still raises ValueError (refused, not probed)."""
    validate_loopback(host)
    try:
        version = get_version(host, port)
        targets = list_targets(host, port)
    except BrowserUnavailableError as exc:
        return {"available": False, "error": str(exc)}
    pages = [
        {"id": t.get("id"), "type": t.get("type"),
         "title": t.get("title"), "url": t.get("url")}
        for t in targets if t.get("type") == "page"
    ]
    return {
        "available": True,
        "browser": version.get("Browser"),
        "protocol_version": version.get("Protocol-Version"),
        "web_socket_debugger_url": version.get("webSocketDebuggerUrl"),
        "targets": pages,
        "target_count": len(pages),
    }


# ----------------------------------------------------------------- session

def _pick_target(targets: list[dict], target_id: str | None) -> dict:
    pages = [t for t in targets if t.get("type") == "page"]
    if target_id is not None:
        for page in pages:
            if page.get("id") == target_id:
                return page
        raise CdpError(f"no page target with id {target_id!r}")
    if not pages:
        raise CdpError("Chrome has no open page target — open a tab first")
    return pages[0]


def _parse_ws_url(ws_url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlparse(ws_url)
    if parsed.scheme != "ws":
        raise CdpError(f"unexpected debugger URL scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    validate_loopback(host)
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, port, path


class CdpSession:
    """One WebSocket connection to one page target.

    Commands are serialised (send one, wait for its reply, skipping
    event messages). Use as a context manager; the socket is closed
    on exit.
    """

    def __init__(self, host: str = DEFAULT_CDP_HOST,
                 port: int = DEFAULT_CDP_PORT,
                 target_id: str | None = None,
                 timeout: float = COMMAND_TIMEOUT):
        validate_loopback(host)
        self.host = host
        self.port = port
        self.target_id = target_id
        self.timeout = timeout
        self._ws: WebSocketClient | None = None
        self._next_id = 1
        self.target: dict = {}

    def __enter__(self) -> "CdpSession":
        targets = list_targets(self.host, self.port)
        self.target = _pick_target(targets, self.target_id)
        ws_url = self.target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("page target has no webSocketDebuggerUrl")
        host, port, path = _parse_ws_url(ws_url)
        self._ws = WebSocketClient(host, port, path, timeout=self.timeout)
        try:
            self._ws.connect()
        except WebSocketError as exc:
            raise BrowserUnavailableError(
                f"cannot open CDP WebSocket: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def command(self, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        """Send one CDP command and return its ``result`` object.

        Raises CdpError on a protocol error or a timeout."""
        assert self._ws is not None, "session not open"
        cmd_id = self._next_id
        self._next_id += 1
        payload = {"id": cmd_id, "method": method,
                   "params": params or {}}
        try:
            self._ws.send_text(json.dumps(payload))
            message = self._ws.recv_message(
                timeout=self.timeout if timeout is None else timeout)
        except WebSocketClosed as exc:
            raise CdpError(f"CDP connection closed during {method}: {exc}"
                           ) from exc
        except WebSocketError as exc:
            raise CdpError(f"CDP {method} failed: {exc}") from exc
        if not isinstance(message, str):
            raise CdpError(f"CDP {method}: unexpected binary message")
        try:
            reply = json.loads(message)
        except ValueError as exc:
            raise CdpError(f"CDP {method}: bad JSON reply") from exc
        if reply.get("id") != cmd_id:
            # Event notification (method present, no matching id):
            # keep waiting for our reply.
            return self._wait_for(cmd_id, method, timeout)
        if "error" in reply:
            err = reply["error"]
            raise CdpError(
                f"CDP {method} error {err.get('code')}: {err.get('message')}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise CdpError(f"CDP {method}: missing result object")
        return result

    def _wait_for(self, cmd_id: int, method: str,
                  timeout: float | None) -> dict:
        assert self._ws is not None
        while True:
            try:
                message = self._ws.recv_message(
                    timeout=self.timeout if timeout is None else timeout)
            except WebSocketClosed as exc:
                raise CdpError(
                    f"CDP connection closed during {method}: {exc}") from exc
            except WebSocketError as exc:
                raise CdpError(f"CDP {method} failed: {exc}") from exc
            if not isinstance(message, str):
                continue
            try:
                reply = json.loads(message)
            except ValueError:
                continue
            if reply.get("id") != cmd_id:
                continue  # another event; keep waiting
            if "error" in reply:
                err = reply["error"]
                raise CdpError(
                    f"CDP {method} error {err.get('code')}: "
                    f"{err.get('message')}")
            result = reply.get("result")
            if not isinstance(result, dict):
                raise CdpError(f"CDP {method}: missing result object")
            return result


# -------------------------------------------------------------- operations

def validate_navigate_url(url: str) -> str:
    """Allow http/https/about:blank only. file:, data:, chrome: and
    friends are refused — the browser must not bypass the jail."""
    if not url or len(url) > MAX_URL_CHARS:
        raise ValueError("url is empty or too long")
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in _ALLOWED_SCHEMES:
        if not parsed.netloc:
            raise ValueError(f"refusing URL without a host: {url!r}")
        return url
    if url == "about:blank":
        return url
    raise ValueError(
        f"refusing URL with scheme {scheme!r}: only http/https "
        "and about:blank are allowed")


def browser_navigate(host: str, port: int, url: str,
                    target_id: str | None = None) -> dict:
    url = validate_navigate_url(url)
    with CdpSession(host, port, target_id) as session:
        result = session.command("Page.navigate", {"url": url})
        return {"url": url, "frame_id": result.get("frameId"),
                "target_id": session.target.get("id")}


def browser_evaluate(host: str, port: int, expression: str,
                     target_id: str | None = None) -> dict:
    if not expression or len(expression) > MAX_EXPRESSION_CHARS:
        raise ValueError("expression is empty or too long")
    with CdpSession(host, port, target_id) as session:
        result = session.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = str(detail.get("exception", {}).get("description")
                       or detail.get("text") or "evaluation failed")
            return {"value": None, "exception": text[:2000],
                    "truncated": len(text) > 2000,
                    "target_id": session.target.get("id")}
        remote = result.get("result", {})
        value = remote.get("value")
        serialised = json.dumps(value, default=str)
        truncated = len(serialised) > MAX_EVAL_BYTES
        if truncated:
            serialised = serialised[:MAX_EVAL_BYTES]
        return {"value": serialised, "value_json": True,
                "truncated": truncated,
                "target_id": session.target.get("id")}


_CONTENT_JS = (
    "(function(){"
    "var limit=" + str(MAX_CONTENT_JS_CHARS) + ";"
    "return {title: document.title,"
    " text: (document.body ? document.body.innerText : '').slice(0, limit),"
    " html: document.documentElement.outerHTML.slice(0, limit)};"
    "})()"
)


def browser_content(host: str, port: int,
                    target_id: str | None = None) -> dict:
    with CdpSession(host, port, target_id) as session:
        result = session.command("Runtime.evaluate", {
            "expression": _CONTENT_JS,
            "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise CdpError("page content extract failed in the page")
        value = result.get("result", {}).get("value", {})
        if not isinstance(value, dict):
            raise CdpError("page content extract returned unexpected data")
        title = str(value.get("title", ""))
        text = str(value.get("text", ""))
        html = str(value.get("html", ""))
        text_truncated = len(text) > MAX_CONTENT_BYTES
        html_truncated = len(html) > MAX_CONTENT_BYTES
        return {
            "url": session.target.get("url"),
            "title": title,
            "text": text[:MAX_CONTENT_BYTES],
            "html": html[:MAX_CONTENT_BYTES],
            "text_truncated": text_truncated,
            "html_truncated": html_truncated,
            "target_id": session.target.get("id"),
        }


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """Width/height from a PNG IHDR, or (0, 0) when unparseable."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    import struct as _struct
    width, height = _struct.unpack("!II", png[16:24])
    return width, height


def browser_shot(host: str, port: int,
                 target_id: str | None = None) -> dict:
    with CdpSession(host, port, target_id) as session:
        result = session.command("Page.captureScreenshot",
                                 {"format": "png"})
        data = result.get("data", "")
        try:
            png = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            raise CdpError("screenshot returned invalid base64") from exc
        if png[:8] != b"\x89PNG\r\n\x1a\n":
            raise CdpError("screenshot did not return a PNG image")
        width, height = _png_dimensions(png)
        return {"png": png, "width": width, "height": height,
                "bytes": len(png),
                "target_id": session.target.get("id")}


def browser_click(host: str, port: int, x: int, y: int,
                  target_id: str | None = None) -> dict:
    if not isinstance(x, int) or not isinstance(y, int):
        raise ValueError("x and y must be integers")
    if x < 0 or y < 0 or x > 65535 or y > 65535:
        raise ValueError("x and y must be in range 0..65535")
    with CdpSession(host, port, target_id) as session:
        for event_type in ("mousePressed", "mouseReleased"):
            session.command("Input.dispatchMouseEvent", {
                "type": event_type, "x": x, "y": y,
                "button": "left", "clickCount": 1,
            })
        return {"x": x, "y": y, "target_id": session.target.get("id")}


def browser_type(host: str, port: int, text: str,
                 target_id: str | None = None) -> dict:
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    if len(text) > MAX_TYPE_CHARS:
        raise ValueError(
            f"text exceeds {MAX_TYPE_CHARS} characters")
    with CdpSession(host, port, target_id) as session:
        session.command("Input.insertText", {"text": text})
        return {"text_len": len(text),
                "target_id": session.target.get("id")}
