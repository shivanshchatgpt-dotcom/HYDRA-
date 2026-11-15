"""Fake Chrome DevTools Protocol server for HYDRA v0.3 tests.

Stdlib sockets only. Serves:

* GET /json/version and /json/list over plain HTTP (the endpoints
  real Chrome exposes on the remote-debugging port), and
* a WebSocket upgrade endpoint that speaks just enough CDP to
  exercise hydra/browser.py: Page.navigate, Runtime.evaluate,
  Page.captureScreenshot, Input.dispatchMouseEvent, Input.insertText.

The fake server is INDEPENDENT of hydra/websocket.py: its frame
parsing/encoding is written from the RFC here, so the tests check
real interoperability, not agreement with itself.

Also provides frame helpers + a PNG builder for the WebSocket
unit tests, which use raw socketpairs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def make_png(width: int = 2, height: int = 2) -> bytes:
    """A valid minimal RGB PNG (all black), built from scratch."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (struct.pack("!I", len(data)) + ctype + data
                + struct.pack("!I", zlib.crc32(ctype + data) & 0xFFFFFFFF))
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def encode_frame_raw(opcode: int, payload: bytes, *, fin: bool = True,
                     mask_key: bytes | None = None) -> bytes:
    """Encode a frame without hydra/websocket.py (independent impl)."""
    length = len(payload)
    head = bytearray([(0x80 if fin else 0x00) | (opcode & 0x0F)])
    mask_bit = 0x80 if mask_key is not None else 0x00
    if length < 126:
        head.append(mask_bit | length)
    elif length <= 0xFFFF:
        head.append(mask_bit | 126)
        head.extend(struct.pack("!H", length))
    else:
        head.append(mask_bit | 127)
        head.extend(struct.pack("!Q", length))
    if mask_key is not None:
        head.extend(mask_key)
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def read_frame_raw(rfile) -> tuple[bool, int, bytes]:
    """Read one frame from a buffered reader. Returns (fin, opcode,
    payload), unmasking when the mask bit is set."""
    head = rfile.read(2)
    if len(head) < 2:
        raise EOFError("connection closed")
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", rfile.read(8))[0]
    mask_key = rfile.read(4) if masked else b""
    payload = rfile.read(length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def read_exact_raw(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise EOFError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def read_frame_from_socket(sock: socket.socket) -> tuple[bool, int, bytes]:
    """Read one frame from a raw socket (for socketpair tests)."""
    head = read_exact_raw(sock, 2)
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact_raw(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact_raw(sock, 8))[0]
    mask_key = read_exact_raw(sock, 4) if masked else b""
    payload = read_exact_raw(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class FakeCDPServer:
    """A fake Chrome on 127.0.0.1 with one page target.

    ``send_event_before_reply`` makes the server emit a CDP event
    notification before each command reply, exercising the client's
    event-skipping. ``page_targets`` may be set to [] to simulate a
    Chrome with no open tabs.
    """

    def __init__(self, send_event_before_reply: bool = False,
                 page_targets: list[dict] | None = None):
        self.send_event_before_reply = send_event_before_reply
        self._page_targets = page_targets  # None -> default one page
        self.commands: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send_json(self, payload, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/json/version":
                    self._send_json({
                        "Browser": "FakeChrome/1.0",
                        "Protocol-Version": "1.3",
                        "webSocketDebuggerUrl":
                            f"ws://127.0.0.1:{outer.port}/devtools/browser/x",
                    })
                elif self.path == "/json/list":
                    targets = outer._targets()
                    self._send_json(targets)
                elif self.headers.get("Upgrade", "").lower() == "websocket":
                    self._handle_websocket()
                else:
                    self.send_error(404)

            def _handle_websocket(self):
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + GUID).encode("ascii")).digest()
                ).decode("ascii")
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                rfile = self.rfile
                wfile = self.wfile
                try:
                    while True:
                        fin, opcode, payload = read_frame_raw(rfile)
                        if opcode == 0x8:  # close
                            wfile.write(encode_frame_raw(0x8, b""))
                            wfile.flush()
                            return
                        if opcode == 0x9:  # ping -> pong
                            wfile.write(encode_frame_raw(0xA, payload))
                            wfile.flush()
                            continue
                        if opcode != 0x1:
                            continue
                        try:
                            command = json.loads(payload.decode("utf-8"))
                        except ValueError:
                            continue
                        with outer._lock:
                            outer.commands.append(command)
                        if outer.send_event_before_reply:
                            event = json.dumps({
                                "method": "Page.loadEventFired",
                                "params": {"timestamp": 1.0},
                            }).encode("utf-8")
                            wfile.write(encode_frame_raw(0x1, event))
                            wfile.flush()
                        reply = outer._reply_for(command)
                        wfile.write(encode_frame_raw(
                            0x1, json.dumps(reply).encode("utf-8")))
                        wfile.flush()
                except (EOFError, OSError, ValueError):
                    return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)

    def _targets(self) -> list[dict]:
        if self._page_targets is not None:
            return self._page_targets
        return [{
            "id": "page-1",
            "type": "page",
            "title": "Fake Page",
            "url": "https://example.com/",
            "webSocketDebuggerUrl":
                f"ws://127.0.0.1:{self.port}/devtools/page/page-1",
        }]

    def _reply_for(self, command: dict) -> dict:
        cmd_id = command.get("id")
        method = command.get("method", "")
        params = command.get("params", {})
        if method == "Page.navigate":
            return {"id": cmd_id, "result": {"frameId": "frame-1"}}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if "outerHTML" in expression:
                value = {"title": "Fake Page",
                         "text": "hello fake world",
                         "html": "<html><body>fake</body></html>"}
                return {"id": cmd_id, "result": {
                    "result": {"type": "object", "value": value}}}
            if "throw" in expression:
                return {"id": cmd_id, "result": {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "Error: boom"}}}}
            if expression.strip() == "1+1":
                return {"id": cmd_id, "result": {
                    "result": {"type": "number", "value": 2}}}
            if "document.title" in expression:
                return {"id": cmd_id, "result": {
                    "result": {"type": "string", "value": "Fake Page"}}}
            return {"id": cmd_id, "result": {
                "result": {"type": "string", "value": expression}}}
        if method == "Page.captureScreenshot":
            png = make_png(2, 2)
            return {"id": cmd_id, "result": {
                "data": base64.b64encode(png).decode("ascii")}}
        if method in ("Input.dispatchMouseEvent", "Input.insertText"):
            return {"id": cmd_id, "result": {}}
        return {"id": cmd_id, "error": {
            "code": -32601, "message": f"method not found: {method}"}}

    def start(self) -> "FakeCDPServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
