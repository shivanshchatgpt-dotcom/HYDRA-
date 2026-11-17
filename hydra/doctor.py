"""Read-only doctor checks (HYDRA v0.4, Tailscale-auto).

``run_doctor`` inspects the laptop's readiness for HYDRA work and
returns one pass/warn/fail check per item, each with a short fix hint
— a command the USER runs by hand. The doctor NEVER fixes anything:
no installs, no service changes, no file writes, no token reads.

Secrecy rule: the token-file check uses ``os.stat`` ONLY (existence +
permission bits). The token file's CONTENTS are never read, so the
token value cannot appear in any doctor output, log, or audit entry.
The same holds for Tailscale: status comes through
``hydra.tailscale``, which drops keys and peer records before
anything is returned.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
from pathlib import Path

from .tailscale import (
    TailscaleError,
    TailscaleUnavailableError,
    tailscale_status,
)

# Disk floor: warn below 5 GiB free (a full Arch upgrade + renders
# need headroom; Baji's laptop once had only 4.7 GiB free and a full
# `pacman -Syu` failed for lack of space), fail below 1 GiB.
WARN_FREE_BYTES = 5 * 1024 * 1024 * 1024
FAIL_FREE_BYTES = 1 * 1024 * 1024 * 1024

# (binary, Arch package, why it matters, missing -> status)
_TOOL_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    ("melt", "mlt", "/video/render/* endpoints", "warn"),
    ("ffmpeg", "ffmpeg", "/video/probe and /video/preview", "warn"),
    ("ffprobe", "ffmpeg", "/video/probe and /video/preview", "warn"),
    ("kdenlive", "kdenlive", "opening .mlt projects by hand (headless renders work without it)", "warn"),
    ("bwrap", "bubblewrap", "sandboxed /exec and /jobs/start", "warn"),
    ("rg", "ripgrep", "fast /search (grep fallback otherwise)", "warn"),
)

_SYSTEMCTL_TIMEOUT = 5.0


def _check(name: str, status: str, detail: str,
           fix_hint: str | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail,
            "fix_hint": fix_hint}


def _tool_check(binary: str, package: str, purpose: str,
                missing_status: str) -> dict:
    if shutil.which(binary) is not None:
        return _check(f"tool_{binary}", "pass",
                      f"{binary} is installed ({purpose}).")
    return _check(f"tool_{binary}", missing_status,
                  f"{binary} is NOT installed; {purpose} unavailable.",
                  f"Install it yourself with: sudo pacman -S {package}")


def _disk_check(workspace: Path) -> dict:
    try:
        usage = shutil.disk_usage(str(workspace))
    except OSError as exc:
        return _check("disk_free", "fail",
                      f"could not read disk usage for {workspace}: {exc}",
                      "Check the workspace filesystem by hand: df -h")
    free_gib = usage.free / (1024 ** 3)
    detail = (f"{free_gib:.1f} GiB free on the workspace filesystem "
              f"({usage.free} bytes free of {usage.total}).")
    if usage.free < FAIL_FREE_BYTES:
        return _check("disk_free", "fail", detail,
                      "Free disk space before any render or upgrade; "
                      "do NOT run a full pacman -Syu while space is "
                      "this low (see docs/TAILSCALE_AUTO.md).")
    if usage.free < WARN_FREE_BYTES:
        return _check("disk_free", "warn", detail,
                      "Disk space is tight: free space before large "
                      "renders, and do NOT run a full pacman -Syu "
                      "until there is more headroom (see "
                      "docs/TAILSCALE_AUTO.md).")
    return _check("disk_free", "pass", detail)


def _token_check(token_file: Path | None) -> dict:
    """Existence + permission bits ONLY. The file is never opened,
    so its contents (the token) can never leak through this check."""
    if token_file is None:
        return _check("token_permissions", "fail",
                      "token file path is not configured for this "
                      "daemon instance.",
                      "Start the daemon with an explicit --token-file "
                      "or HYDRA_TOKEN_FILE.")
    try:
        mode = os.stat(token_file).st_mode & 0o777
    except FileNotFoundError:
        return _check("token_permissions", "fail",
                      f"token file does not exist: {token_file}",
                      "Generate one with: python3 "
                      "scripts/generate_token.py")
    except OSError as exc:
        return _check("token_permissions", "fail",
                      f"could not stat token file {token_file}: {exc}",
                      "Check the token file by hand; the daemon "
                      "refuses to start without a 0600 token file.")
    if mode & 0o077:
        return _check("token_permissions", "fail",
                      f"token file {token_file} has insecure "
                      f"permissions {oct(mode)} (must be 0600).",
                      f"Fix it yourself with: chmod 600 {token_file}")
    return _check("token_permissions", "pass",
                  f"token file exists with mode {oct(mode)} "
                  f"(contents never read by the doctor).")


def _systemd_check() -> dict:
    """Read-only `systemctl --user` queries. Never enables, starts,
    or stops anything."""
    if shutil.which("systemctl") is None:
        return _check("systemd_unit", "warn",
                      "systemctl is not installed; unit status unknown.",
                      "Start the daemon by hand with: "
                      "./scripts/start-daemon.sh")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", ""), "LC_ALL": "C"}
    try:
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", "hydra"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_SYSTEMCTL_TIMEOUT)
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "hydra"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_SYSTEMCTL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _check("systemd_unit", "warn",
                      f"systemctl queries failed: {exc}",
                      "Check by hand: systemctl --user status hydra")
    enabled_state = enabled.stdout.strip() or "unknown"
    active_state = active.stdout.strip() or "unknown"
    detail = (f"hydra unit: enabled={enabled_state}, "
              f"active={active_state}.")
    if enabled_state == "enabled" and active_state == "active":
        return _check("systemd_unit", "pass", detail)
    return _check(
        "systemd_unit", "warn", detail,
        "Autostart is opt-in and NOT installed by setup. To install "
        "it yourself, see docs/TAILSCALE_AUTO.md (or run "
        "scripts/setup-tailscale.sh --enable-autostart on the "
        "laptop). Starting the daemon by hand also works.")


def _bind_check(bind: str, bind_mode: str, port: int) -> dict:
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return _check("bind_mode", "fail",
                      f"daemon bind address {bind!r} is not an IP "
                      "address.",
                      "Restart the daemon with --bind 127.0.0.1 or "
                      "--bind tailscale.")
    if ip.is_loopback:
        return _check("bind_mode", "pass",
                      f"daemon binds {bind}:{port} (loopback only; "
                      "Mikey cannot reach it over Tailscale).",
                      "For Tailscale access, restart with: "
                      "--bind tailscale (see docs/TAILSCALE_AUTO.md).")
    tailscale_net = ipaddress.ip_network("100.64.0.0/10")
    if ip in tailscale_net:
        return _check("bind_mode", "pass",
                      f"daemon binds {bind}:{port} (Tailscale IP; "
                      f"bind mode: {bind_mode}).")
    return _check("bind_mode", "fail",
                  f"daemon binds {bind}:{port}, which is neither "
                  "loopback nor a Tailscale IP.",
                  "Restart the daemon with --bind 127.0.0.1 or "
                  "--bind tailscale; never bind a public interface.")


def run_doctor(workspace: Path, data_dir: Path | None,
               token_file: Path | None, bind: str, port: int,
               bind_mode: str = "explicit") -> dict:
    """Run every check. Never raises for a missing tool or a failed
    probe: each failure is a check with status fail/warn, so the
    caller always gets a complete report."""
    checks: list[dict] = []

    # Tailscale presence + connection (secret-free, via hydra.tailscale).
    if shutil.which("tailscale") is None:
        checks.append(_check(
            "tailscale_present", "fail",
            "tailscale is NOT installed; the daemon is reachable "
            "on localhost only.",
            "Install it yourself with: sudo pacman -S tailscale "
            "(see docs/TAILSCALE_AUTO.md)."))
        checks.append(_check(
            "tailscale_connected", "fail",
            "Tailscale status unknown: tailscale is not installed.",
            "Install Tailscale first, then run: sudo tailscale up"))
    else:
        checks.append(_check("tailscale_present", "pass",
                             "tailscale is installed."))
        try:
            status = tailscale_status()
        except TailscaleUnavailableError:
            checks.append(_check(
                "tailscale_connected", "fail",
                "tailscale binary disappeared between checks.",
                "Install it yourself with: sudo pacman -S tailscale"))
        except TailscaleError as exc:
            checks.append(_check("tailscale_connected", "fail",
                                 f"Tailscale is down: {exc}",
                                 "Bring it up yourself with: "
                                 "sudo tailscale up (see "
                                 "docs/TAILSCALE_AUTO.md)."))
        else:
            if status["connected"]:
                checks.append(_check(
                    "tailscale_connected", "pass",
                    f"Tailscale is Running as "
                    f"{status['self_hostname'] or 'unknown'} "
                    f"({status['self_ip']}), "
                    f"{status['peer_count']} peer(s) visible."))
            else:
                checks.append(_check(
                    "tailscale_connected", "fail",
                    "Tailscale is not connected "
                    f"(backend state: "
                    f"{status['backend_state'] or 'unknown'}).",
                    "Bring it up yourself with: sudo tailscale up "
                    "(see docs/TAILSCALE_AUTO.md)."))

    # Optional tools (each missing tool only gates its endpoints).
    for binary, package, purpose, missing_status in _TOOL_CHECKS:
        checks.append(_tool_check(binary, package, purpose, missing_status))

    checks.append(_disk_check(workspace))
    checks.append(_token_check(token_file))

    if workspace.is_dir():
        checks.append(_check("workspace", "pass",
                             f"workspace exists: {workspace}"))
    else:
        checks.append(_check("workspace", "fail",
                             f"workspace does not exist: {workspace}",
                             "Create it yourself, or restart the "
                             "daemon (it creates the workspace)."))

    checks.append(_systemd_check())
    checks.append(_bind_check(bind, bind_mode, port))

    summary = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        summary[check["status"]] += 1
    overall = ("fail" if summary["fail"]
               else "warn" if summary["warn"] else "pass")
    return {
        "bind": bind,
        "bind_mode": bind_mode,
        "port": port,
        "workspace": str(workspace),
        "data_dir": str(data_dir) if data_dir is not None else None,
        "checks": checks,
        "summary": summary,
        "overall": overall,
    }
