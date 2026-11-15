"""Optional bubblewrap sandbox for /exec and background jobs.

When the caller asks for ``sandbox: true``, the daemon wraps the
approved shell command in ``bwrap`` (bubblewrap) instead of running it
directly:

  * the whole filesystem is mounted READ-ONLY (``--ro-bind / /``),
  * the workspace is bound READ-WRITE on top of that,
  * ``/tmp`` is a fresh tmpfs, ``/proc`` and ``/dev`` are fresh mounts,
  * user/pid/uts/ipc/cgroup namespaces are unshared,
  * network is unshared too, unless the caller opts out.

The command string itself still goes through the normal blocklist
first: the sandbox is a second layer, not a replacement. If ``bwrap``
is not installed, sandboxed execution fails with a clear error naming
the missing tool — it NEVER silently runs unsandboxed.

This module only builds argv; it never runs anything on import.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    """Raised when a sandbox was requested but cannot be built."""


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def build_bwrap_argv(
    command: str,
    workspace: str | Path,
    cwd: str | Path,
    *,
    no_network: bool = True,
) -> list[str]:
    """Build the bwrap argv for ``/bin/sh -c <command>``.

    Raises SandboxUnavailableError when bwrap is missing. ``workspace``
    and ``cwd`` must already be jail-resolved absolute paths.
    """
    bwrap = bwrap_path()
    if not bwrap:
        raise SandboxUnavailableError(
            "sandbox unavailable: bubblewrap (bwrap) is not installed; "
            "install it with: sudo pacman -S bubblewrap"
        )
    ws = str(Path(workspace))
    argv: list[str] = [
        bwrap,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--bind", ws, ws,
        "--tmpfs", "/tmp",
        "--proc", "/proc",
        "--dev", "/dev",
        "--chdir", str(Path(cwd)),
    ]
    if no_network:
        argv.append("--unshare-net")
    argv += ["/bin/sh", "-c", command]
    return argv
