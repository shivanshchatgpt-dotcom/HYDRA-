"""Read-only system and git information (HYDRA v0.3).

Two endpoints, both strictly read-only:

* ``/system/info`` — disk space on the workspace filesystem, load
  average, memory from /proc/meminfo, uptime, and whether the tools
  HYDRA can use (rg, bwrap, ffmpeg, ydotool, spectacle, grim) are
  installed. Nothing here can change the system.
* ``/git/status`` — branch, upstream ahead/behind counts, and a
  short porcelain status for a directory inside the workspace.
  ``GIT_OPTIONAL_LOCKS=0`` is set so even the git index is never
  locked or refreshed for writing. No commit, push, fetch, or
  checkout behaviour exists anywhere in this module — those stay
  behind the normal /exec blocklist checks.

Both functions are pure reads with fixed argument lists: no shell,
no user-supplied command strings, no network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .security import resolve_inside

TOOL_NAMES = ("rg", "bwrap", "ffmpeg", "ydotool", "spectacle", "grim")
_GIT_TIMEOUT = 10.0
_MAX_CHANGES = 200


class GitUnavailableError(RuntimeError):
    """git is not installed, or the command could not run."""


def system_info(workspace: Path) -> dict:
    """Disk / load / memory / uptime / tool presence. Never raises
    for a missing data source: unavailable fields are ``None``."""
    try:
        usage = shutil.disk_usage(str(workspace))
        disk = {"path": str(workspace), "total_bytes": usage.total,
                "used_bytes": usage.used, "free_bytes": usage.free}
    except OSError:
        disk = {"path": str(workspace), "total_bytes": None,
                "used_bytes": None, "free_bytes": None}
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):
        load = None
    memory = _meminfo()
    uptime = _uptime()
    tools = {name: shutil.which(name) is not None for name in TOOL_NAMES}
    return {"disk": disk, "load_average": load, "memory": memory,
            "uptime_seconds": uptime, "tools": tools}


def _meminfo() -> dict | None:
    try:
        lines = Path("/proc/meminfo").read_text(
            encoding="ascii", errors="replace").splitlines()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024  # kB -> bytes
    if "MemTotal" not in values:
        return None
    return {"total_bytes": values.get("MemTotal"),
            "available_bytes": values.get("MemAvailable"),
            "free_bytes": values.get("MemFree")}


def _uptime() -> float | None:
    try:
        first = Path("/proc/uptime").read_text(
            encoding="ascii").split()[0]
        return float(first)
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------- git

def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run one fixed git command. Read-only by construction: the
    caller only ever passes rev-parse / rev-list / status, and
    GIT_OPTIONAL_LOCKS=0 forbids index writes."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            ["git"] + args, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=_GIT_TIMEOUT)
    except FileNotFoundError as exc:
        raise GitUnavailableError(
            "git is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailableError(
            f"git {' '.join(args)} timed out") from exc


def git_status(workspace: Path, rel_path: str = ".") -> dict:
    """Read-only git status for a directory inside the workspace.

    Raises ValueError when the path escapes the workspace,
    FileNotFoundError when it does not exist, GitUnavailableError
    when git is missing or unusable. A directory that is not a
    git repository returns ``{"is_repo": false, ...}`` — that is a
    normal answer, not an error.
    """
    resolved = resolve_inside(workspace, rel_path or ".")
    if not resolved.exists():
        raise FileNotFoundError(f"no such path: {rel_path!r}")
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {rel_path!r}")
    if shutil.which("git") is None:
        raise GitUnavailableError("git is not installed on this machine")

    probe = _run_git(["rev-parse", "--git-dir"], resolved)
    if probe.returncode != 0:
        return {"is_repo": False, "path": str(resolved),
                "branch": None, "head": None, "upstream": None,
                "ahead": None, "behind": None,
                "changes": [], "change_count": 0, "truncated": False}

    head = _run_git(["rev-parse", "HEAD"], resolved).stdout.strip() or None
    branch_out = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], resolved).stdout.strip()
    detached = branch_out in ("", "HEAD")
    branch = None if detached else branch_out

    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    up = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        resolved)
    if up.returncode == 0 and up.stdout.strip():
        upstream = up.stdout.strip()
        counts = _run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            resolved)
        if counts.returncode == 0:
            parts = counts.stdout.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                ahead, behind = int(parts[0]), int(parts[1])

    status = _run_git(["status", "--porcelain=v1"], resolved)
    changes: list[dict] = []
    if status.returncode == 0:
        lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        truncated = len(lines) > _MAX_CHANGES
        for line in lines[:_MAX_CHANGES]:
            changes.append({"status": line[:2].strip() or "?",
                            "path": line[3:] if len(line) > 3 else ""})
    else:
        truncated = False
        lines = []

    return {"is_repo": True, "path": str(resolved), "branch": branch,
            "detached": detached, "head": head, "upstream": upstream,
            "ahead": ahead, "behind": behind, "changes": changes,
            "change_count": len(lines) if status.returncode == 0 else 0,
            "truncated": truncated}
