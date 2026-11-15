"""Minimal stdlib-only WebSocket client (RFC 6455), for CDP.

HYDRA talks to Chrome's DevTools Protocol over a local WebSocket.
The daemon has no third-party dependencies, so this client is
hand-rolled on top of ``socket``. It implements exactly what CDP
needs:

* HTTP/1.1 Upgrade handshake with Sec-WebSocket-Key/Accept check.
* Client-masked text frames (clients MUST mask; servers MUST NOT).
* Fragmented messages (continuation frames), ping/pong, close.
* 7-bit, 16-bit and 64-bit payload lengths.
* A receive timeout per call, so a dead browser can never hang the
  daemon thread forever.

No TLS (CDP runs on loopback only — see hydra/browser.py, which
refuses non-loopback targets). No extensions, no subprotocols.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import time

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_MAX_HEADER_BYTES = 64 * 1024
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024  # 16MB: screenshots can be large


class WebSocketError(RuntimeError):
    """Protocol or transport failure."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection (or it went away)."""


def _mask_payload(mask_key: bytes, payload: bytes) -> bytes:
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def encode_frame(opcode: int, payload: bytes, *, fin: bool = True,
                 mask: bool = True) -> bytes:
    """Encode one frame. Clients must mask; the ``mask`` parameter
    exists so tests can build server (unmasked) frames."""
    length = len(payload)
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length <= 0xFFFF:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if mask:
        key = os.urandom(4)
        header.extend(key)
        return bytes(header) + _mask_payload(key, payload)
    return bytes(header) + payload


class WebSocketClient:
    """A single WebSocket connection. Not thread-safe: one thread
    sends and receives (the CDP session in hydra/browser.py owns
    the client and serialises commands)."""

    def __init__(self, host: str, port: int, path: str = "/",
                 timeout: float = 10.0):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._closed = False

    # -- connection ----------------------------------------------------

    def connect(self) -> None:
        """Open the TCP connection and complete the handshake."""
        if self._sock is not None:
            raise WebSocketError("already connected")
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise WebSocketError(
                f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            sock.sendall(request.encode("ascii"))
            header_blob = self._read_until(sock, b"\r\n\r\n")
        except (OSError, WebSocketError) as exc:
            sock.close()
            raise WebSocketError(
                f"WebSocket handshake failed: {exc}") from exc
        headers, _, rest = header_blob.partition(b"\r\n\r\n")
        self._buf.extend(rest)
        lines = headers.decode("latin-1").split("\r\n")
        status = lines[0] if lines else ""
        if " 101 " not in f" {status} " and not status.endswith(" 101"):
            sock.close()
            raise WebSocketError(
                f"WebSocket handshake rejected: {status!r}")
        fields = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                fields[name.strip().lower()] = value.strip().lower()
        if fields.get("upgrade") != "websocket":
            sock.close()
            raise WebSocketError(
                "WebSocket handshake missing 'Upgrade: websocket'")
        accept = fields.get("sec-websocket-accept", "")
        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()
        ).decode("ascii").lower()
        if accept != expected:
            sock.close()
            raise WebSocketError("WebSocket handshake: bad Sec-WebSocket-Accept")
        self._sock = sock

    def _read_until(self, sock: socket.socket, marker: bytes) -> bytes:
        blob = bytearray(self._buf)
        self._buf.clear()
        deadline = time.monotonic() + self.timeout
        while marker not in blob:
            if len(blob) > _MAX_HEADER_BYTES:
                raise WebSocketError("handshake response too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSocketError("handshake timed out")
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise WebSocketError("handshake timed out") from exc
            if not chunk:
                raise WebSocketClosed("connection closed during handshake")
            blob.extend(chunk)
        return bytes(blob)

    # -- framing ---------------------------------------------------------

    def _read_exact(self, count: int) -> bytes:
        """Read exactly ``count`` bytes, honouring the receive
        deadline. Raises WebSocketClosed on EOF, WebSocketError
        on timeout."""
        assert self._sock is not None
        while len(self._buf) < count:
            try:
                chunk = self._sock.recv(min(65536, count - len(self._buf)))
            except socket.timeout as exc:
                raise WebSocketError("read timed out") from exc
            except OSError as exc:
                raise WebSocketClosed(f"connection lost: {exc}") from exc
            if not chunk:
                raise WebSocketClosed("connection closed by peer")
            self._buf.extend(chunk)
        out = bytes(self._buf[:count])
        del self._buf[:count]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """Read one frame. Returns (fin, opcode, payload)."""
        head = self._read_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > _MAX_MESSAGE_BYTES:
            raise WebSocketError(f"frame too large ({length} bytes)")
        mask_key = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = _mask_payload(mask_key, payload)
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes,
                    *, fin: bool = True) -> None:
        if self._sock is None or self._closed:
            raise WebSocketClosed("not connected")
        try:
            self._sock.sendall(
                encode_frame(opcode, payload, fin=fin, mask=True))
        except OSError as exc:
            raise WebSocketClosed(f"send failed: {exc}") from exc

    # -- messages --------------------------------------------------------

    def send_text(self, data: str) -> None:
        self._send_frame(OP_TEXT, data.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    def recv_message(self, timeout: float | None = None) -> str | bytes:
        """Read one complete message, re-assembling fragments.

        Returns ``str`` for text messages, ``bytes`` for binary.
        Ping frames are answered with a pong automatically; pong
        frames are ignored. A close frame is answered and raises
        WebSocketClosed. Raises WebSocketError on timeout."""
        if self._sock is None or self._closed:
            raise WebSocketClosed("not connected")
        old_timeout = self._sock.gettimeout()
        self._sock.settimeout(self.timeout if timeout is None else timeout)
        try:
            message: bytearray | None = None
            message_opcode = OP_TEXT
            while True:
                fin, opcode, payload = self._read_frame()
                if opcode == OP_CLOSE:
                    try:
                        self._send_frame(OP_CLOSE, b"")
                    except WebSocketError:
                        pass
                    self._closed = True
                    raise WebSocketClosed("peer closed the connection")
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode in (OP_TEXT, OP_BINARY):
                    if message is not None:
                        raise WebSocketError(
                            "new data frame before previous message finished")
                    message = bytearray(payload)
                    message_opcode = opcode
                elif opcode == OP_CONT:
                    if message is None:
                        raise WebSocketError(
                            "continuation frame with no open message")
                    message.extend(payload)
                else:
                    raise WebSocketError(f"unknown opcode {opcode}")
                if len(message) > _MAX_MESSAGE_BYTES:
                    raise WebSocketError("message too large")
                if fin:
                    assert message is not None
                    if message_opcode == OP_TEXT:
                        return bytes(message).decode(
                            "utf-8", errors="replace")
                    return bytes(message)
        finally:
            try:
                self._sock.settimeout(old_timeout)
            except OSError:
                pass

    # -- teardown ----------------------------------------------------------

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame, wait briefly for the peer's close,
        then close the socket. Idempotent; never raises."""
        if self._sock is None:
            return
        if not self._closed:
            try:
                self._sock.settimeout(0.5)
                self._send_frame(
                    OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8"))
                try:
                    self._read_frame()
                except (WebSocketError, OSError):
                    pass
            except (WebSocketError, OSError):
                pass
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def __enter__(self) -> "WebSocketClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
