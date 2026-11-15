"""Best-effort GUI endpoints for KDE/Wayland: screenshots + input.

These endpoints are HONEST about failure: when the needed tool or
permission is missing, they return a clear error naming it. They
never fake a screenshot and never pretend a click happened.

Screenshot backends, tried in order:
  1. ``spectacle`` (KDE's own tool):  spectacle -b -f -o <file>
  2. ``grim`` (wlroots screencopy; often fails under KWin, so it is
     only a fallback attempt)
  3. xdg-desktop-portal Screenshot via ``gdbus`` (may show a system
     permission dialog on first use; needs a Wayland session)

Input goes through ``ydotool`` only, which needs the ``ydotoold``
daemon running and the user in the ``input`` group. See
docs/GUI_SETUP.md for the exact laptop setup and permissions.

The daemon MUST be started from inside the graphical session
(a Konsole window on Baji's desktop) for these endpoints to work:
the tools need WAYLAND_DISPLAY / DBUS_SESSION_BUS_ADDRESS from the
session environment. A daemon started over ssh or from a systemd
unit without that environment will fail with a clear error.

NO model code, NO network calls. Typed text is never logged (only
its length/hash, per the audit rules).
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_TIMEOUT = 30
PORTAL_TIMEOUT = 25
INPUT_TIMEOUT = 15
MAX_TYPE_CHARS = 4000

_YDOTOOL_BUTTONS = {"left": "0xC0", "middle": "0xC1", "right": "0xC2"}


class GuiUnavailableError(RuntimeError):
    """The GUI action cannot run on this host (tool/permission missing)."""


class GuiInputError(ValueError):
    """The caller's input arguments were invalid."""


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Width/height from the PNG IHDR chunk (None when unparseable)."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC):
        return None, None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None, None
    return width, height


def _read_png(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < len(PNG_MAGIC) or not data.startswith(PNG_MAGIC):
        return None
    return data


def _session_env() -> dict[str, str]:
    return dict(os.environ)


def _try_spectacle(tmp_path: Path) -> tuple[bytes | None, str]:
    exe = shutil.which("spectacle")
    if not exe:
        return None, "spectacle is not installed (sudo pacman -S spectacle)"
    try:
        proc = subprocess.run(
            [exe, "-b", "-f", "-o", str(tmp_path)],
            capture_output=True, text=True, timeout=SCREENSHOT_TIMEOUT,
            env=_session_env(), errors="replace")
    except subprocess.TimeoutExpired:
        return None, "spectacle timed out"
    data = _read_png(tmp_path)
    if proc.returncode == 0 and data:
        return data, ""
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    return None, f"spectacle failed (exit {proc.returncode}): {detail}"


def _try_grim(tmp_path: Path) -> tuple[bytes | None, str]:
    exe = shutil.which("grim")
    if not exe:
        return None, "grim is not installed (sudo pacman -S grim)"
    try:
        proc = subprocess.run(
            [exe, str(tmp_path)],
            capture_output=True, text=True, timeout=SCREENSHOT_TIMEOUT,
            env=_session_env(), errors="replace")
    except subprocess.TimeoutExpired:
        return None, "grim timed out"
    data = _read_png(tmp_path)
    if proc.returncode == 0 and data:
        return data, ""
    detail = (proc.stderr or proc.stdout or "").strip()[:200]
    return None, f"grim failed (exit {proc.returncode}): {detail}"


def _try_portal(tmp_path: Path) -> tuple[bytes | None, str]:
    """xdg-desktop-portal Screenshot via gdbus (best effort).

    Calls Screenshot with a unique handle token, then watches the
    request object for the Response signal carrying the file URI.
    On KDE this may pop a permission dialog the first time; if the
    user dismisses it (or no session bus exists), this fails with a
    clear reason instead of hanging.
    """
    exe = shutil.which("gdbus")
    if not exe:
        return None, "gdbus is not installed (sudo pacman -S glib2)"
    token = "hydra" + uuid.uuid4().hex[:12]
    options = f"{{'handle_token': <'{token}'>, 'interactive': <false>}}"
    try:
        call = subprocess.run(
            [exe, "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Screenshot.Screenshot",
             "", options],
            capture_output=True, text=True, timeout=15,
            env=_session_env(), errors="replace")
    except subprocess.TimeoutExpired:
        return None, "portal Screenshot call timed out"
    if call.returncode != 0:
        detail = (call.stderr or call.stdout or "").strip()[:200]
        return None, f"portal Screenshot call failed: {detail}"
    match = re.search(r"(/org/freedesktop/portal/desktop/request/\S+)",
                      call.stdout)
    if not match:
        return None, "portal did not return a request object path"
    request_path = match.group(1).rstrip(",)")
    monitor = subprocess.Popen(
        [exe, "monitor", "--session",
         "--dest", "org.freedesktop.portal.Desktop",
         "--object-path", request_path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=_session_env(), errors="replace")
    uri: str | None = None
    failure = ""
    deadline = time.monotonic() + PORTAL_TIMEOUT
    try:
        assert monitor.stdout is not None
        while time.monotonic() < deadline:
            line = monitor.stdout.readline()
            if not line:
                break
            if "org.freedesktop.portal.Request.Response" not in line:
                continue
            code = re.search(r"\(uint32 (\d+)", line)
            if code and code.group(1) != "0":
                failure = ("portal Screenshot was denied or cancelled "
                           f"(response {code.group(1)}); approve the "
                           "system permission dialog and retry")
                break
            uri_match = re.search(r"'uri':\s*<'file://([^']+)'>", line)
            if uri_match:
                uri = unquote(urlparse(
                    "file://" + uri_match.group(1)).path)
                break
    finally:
        monitor.kill()
        try:
            monitor.wait(timeout=5)
        except Exception:
            pass
    if failure:
        return None, failure
    if not uri:
        return None, ("portal Screenshot produced no response in "
                      f"{PORTAL_TIMEOUT}s; approve the system permission "
                      "dialog if one appeared, then retry")
    data = _read_png(Path(uri))
    if not data:
        return None, f"portal returned a file that is not a PNG: {uri}"
    try:
        tmp_path.write_bytes(data)
    except OSError:
        pass
    return data, ""


def take_screenshot() -> dict:
    """Capture the screen. Returns {png_bytes, backend, width, height}.

    Raises GuiUnavailableError naming every backend that was tried
    and why each failed. Never returns fake or empty image data.
    """
    env = os.environ
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        raise GuiUnavailableError(
            "no graphical session: neither WAYLAND_DISPLAY nor DISPLAY "
            "is set; start the daemon from a terminal inside the KDE "
            "desktop session (Konsole), not over ssh or systemd"
        )
    attempts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="hydra-shot-") as td:
        tmp_path = Path(td) / "shot.png"
        for name, fn in (("spectacle", _try_spectacle),
                         ("grim", _try_grim),
                         ("xdg-desktop-portal", _try_portal)):
            data, reason = fn(tmp_path)
            if data:
                width, height = _png_dimensions(data)
                return {"png_bytes": data, "backend": name,
                        "width": width, "height": height}
            attempts.append(f"{name}: {reason}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    raise GuiUnavailableError(
        "screenshot failed; every backend failed: " + " | ".join(attempts)
    )


# ---------------------------------------------------------------------------
# Input (ydotool)
# ---------------------------------------------------------------------------

def _ydotool() -> str:
    exe = shutil.which("ydotool")
    if not exe:
        raise GuiUnavailableError(
            "ydotool is not installed; install it with: "
            "sudo pacman -S ydotool, then start the ydotoold daemon and "
            "add your user to the 'input' group (see docs/GUI_SETUP.md)"
        )
    return exe


def _run_ydotool(argv: list[str]) -> None:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=INPUT_TIMEOUT,
            env=_session_env(), errors="replace")
    except subprocess.TimeoutExpired:
        raise GuiUnavailableError("ydotool timed out") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise GuiUnavailableError(
            f"ydotool failed (exit {proc.returncode}): {detail}. "
            "Is ydotoold running and is your user in the 'input' group? "
            "See docs/GUI_SETUP.md."
        )


def _check_coords(x: object, y: object) -> tuple[int, int]:
    if isinstance(x, bool) or isinstance(y, bool):
        raise GuiInputError("x and y must be integers")
    try:
        xi, yi = int(x), int(y)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise GuiInputError("x and y must be integers") from None
    if xi < 0 or yi < 0:
        raise GuiInputError("x and y must be >= 0")
    return xi, yi


def input_move(x: object, y: object) -> dict:
    xi, yi = _check_coords(x, y)
    exe = _ydotool()
    _run_ydotool([exe, "mousemove", "--absolute",
                  "-x", str(xi), "-y", str(yi)])
    return {"ok": True, "action": "move", "x": xi, "y": yi}


def input_click(
    x: object | None, y: object | None,
    button: str = "left", clicks: int = 1,
) -> dict:
    if button not in _YDOTOOL_BUTTONS:
        raise GuiInputError("button must be left, middle, or right")
    try:
        clicks = int(clicks)
    except (TypeError, ValueError):
        raise GuiInputError("clicks must be an integer") from None
    if clicks < 1 or clicks > 3:
        raise GuiInputError("clicks must be 1, 2, or 3")
    # Validate coordinates before checking for ydotool, so bad input
    # is a 400 (client error) even when ydotool is missing (501).
    coords: tuple[int, int] | None = None
    if x is not None or y is not None:
        if x is None or y is None:
            raise GuiInputError("x and y must be given together")
        coords = _check_coords(x, y)
    exe = _ydotool()
    moved: dict[str, int] = {}
    if coords is not None:
        xi, yi = coords
        _run_ydotool([exe, "mousemove", "--absolute",
                      "-x", str(xi), "-y", str(yi)])
        moved = {"x": xi, "y": yi}
    code = _YDOTOOL_BUTTONS[button]
    for i in range(clicks):
        _run_ydotool([exe, "click", code])
        if i + 1 < clicks:
            time.sleep(0.1)
    return {"ok": True, "action": "click", "button": button,
            "clicks": clicks, **moved}


def input_type(text: object) -> dict:
    if not isinstance(text, str) or not text:
        raise GuiInputError("text must be a non-empty string")
    if len(text) > MAX_TYPE_CHARS:
        raise GuiInputError(
            f"text too long (max {MAX_TYPE_CHARS} chars)")
    exe = _ydotool()
    _run_ydotool([exe, "type", "--", text])
    return {"ok": True, "action": "type", "text_len": len(text)}
