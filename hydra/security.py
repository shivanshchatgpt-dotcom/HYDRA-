"""HYDRA security helpers: workspace jail, command blocklist, token handling.

This module is deliberately small and readable so Baji can audit it.
It contains NO model code, NO network calls, NO telemetry.

Rules enforced here:
  * All file access stays inside the workspace (symlinks resolved).
  * Dangerous shell commands are refused BEFORE execution (fail closed).
  * The bearer token is compared in constant time and never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shlex
from pathlib import Path


# ---------------------------------------------------------------------------
# Workspace jail
# ---------------------------------------------------------------------------

def resolve_workspace(workspace: str | Path) -> Path:
    """Return the canonical (symlink-resolved) workspace directory."""
    return Path(workspace).expanduser().resolve()


def resolve_inside(workspace: str | Path, user_path: str) -> Path:
    """Resolve ``user_path`` and require it to stay inside ``workspace``.

    Raises ValueError on any escape attempt: ``..``, absolute paths
    outside the workspace, or symlinks pointing outside. The returned
    path is fully resolved, so callers must use it (not the raw input).
    """
    if not user_path:
        raise ValueError("empty path")
    if "\x00" in user_path:
        raise ValueError("NUL byte in path")

    root = resolve_workspace(workspace)
    candidate = Path(user_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    # resolve() follows symlinks for existing parts; strict=False so
    # not-yet-existing files (for /write) still resolve safely through
    # their existing parent directories.
    resolved = candidate.resolve(strict=False)

    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes workspace: {user_path!r}") from None
    return resolved


def resolve_readable(
    workspace: str | Path, user_path: str, read_only_paths: list[str] | None = None
) -> tuple[Path, bool]:
    """Resolve a path for READ/LIST. Returns (path, is_read_only_area).

    Read-only extra paths (default: none) allow reads outside the
    workspace when the user explicitly configured them. Writes and
    exec cwd must never use this helper.
    """
    try:
        return resolve_inside(workspace, user_path), False
    except ValueError:
        pass
    for extra in read_only_paths or []:
        try:
            return resolve_inside(extra, user_path), True
        except ValueError:
            continue
    raise ValueError(f"path is not readable: {user_path!r}")


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def load_token(token_file: str | Path) -> str:
    """Load the bearer token, refusing insecure file permissions.

    The token file MUST be mode 0600 (or stricter). This fails closed:
    a world/group-readable token file refuses to start the daemon.
    """
    path = Path(token_file).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"token file not found: {path}")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"token file {path} has insecure permissions {oct(mode)}; "
            "run: chmod 600 " + str(path)
        )
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 20:
        raise ValueError("token file contains a token that is too short")
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    """Constant-time bearer token comparison. Never log either value."""
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def sha256_prefix(data: bytes, length: int = 16) -> str:
    """Short hash for audit logs (proves content without storing it)."""
    return hashlib.sha256(data).hexdigest()[:length]


# ---------------------------------------------------------------------------
# Command blocklist (/exec)
# ---------------------------------------------------------------------------
# The blocklist is intentionally visible and conservative. Anything
# ambiguous is REFUSED (fail closed). If a legitimate command is refused,
# Baji can run it himself in his own terminal; the daemon never needs to
# run it. Nothing here is a "smart" judgement: these are fixed rules,
# no model is involved.

#: Commands refused outright when they appear as the first word of any
#: pipeline segment (e.g. "sudo ...", "x | sudo ...").
BANNED_FIRST_WORDS = {
    "sudo", "su", "doas", "pkexec", "run0",  # privilege escalation
    "mkfs", "mkfs.ext4", "mkfs.btrfs", "mkfs.fat", "mkfs.xfs",
    "dd", "fdisk", "parted", "wipefs",  # raw disk / partitioning
    "shutdown", "reboot", "poweroff", "halt",  # power control
    "chown", "chgrp",  # ownership changes
    "iptables", "nft", "firewall-cmd", "ufw",  # firewall changes
    "ssh", "scp", "sftp",  # remote access / exfiltration (v0.1: never)
    "nc", "ncat", "socat", "telnet",  # raw network tools
    "eval",  # hides intent; refused so review stays possible
    # Directory / namespace escapes: "cd .." would move the shell out
    # of the workspace jail for the rest of the command, so directory
    # changes are refused entirely. Use the /exec "cwd" field instead,
    # which is jail-checked before the command runs.
    "cd", "pushd", "popd",
    "chroot", "bwrap", "firejail", "unshare", "nsenter",
    "systemd-run",  # can escape cwd/user/sandbox context
}

#: systemctl verbs that change system state. Read-only verbs such as
#: "status" are allowed. Management needs explicit opt-in config.
SYSTEMCTL_MANAGING_VERBS = {
    "enable", "disable", "start", "stop", "restart", "reload",
    "mask", "unmask", "reenable", "link",
}

#: Regex patterns refused anywhere in the raw command string.
BANNED_PATTERNS = [
    (re.compile(r"\$\(\s*[^)]*\)"), "command substitution $(...) is refused"),
    (re.compile(r"`[^`]*`"), "backtick command substitution is refused"),
    (re.compile(r"<\(|>\("), "process substitution <(...) is refused"),
    (re.compile(r"\|\s*(sh|bash|zsh|fish|python3?|perl|ruby)\b"),
     "piping into a shell/interpreter (curl|sh style) is refused"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh)\b"),
     "curl/wget piped to a shell is refused"),
    (re.compile(r"\brm\s+[^;|&]*-[a-zA-Z]*r[a-zA-Z]*f"), "rm -rf pattern refused; check target"),
    (re.compile(r"\brm\s+[^;|&]*-[a-zA-Z]*f[a-zA-Z]*r"), "rm -fr pattern refused; check target"),
    (re.compile(r"\bchmod\s+[^;|&]*\b(0?777|a\+rwx)\b"), "chmod 777 is refused"),
    (re.compile(r"\.ssh\b"), "paths under .ssh are refused"),
    (re.compile(r"authorized_keys"), "authorized_keys is refused"),
    (re.compile(r"\bid_(rsa|ed25519|ecdsa|dsa)\b"), "private key filenames are refused"),
]

#: Path tokens that are never acceptable as command arguments.
NEVER_PATHS = {"/", "/etc", "/boot", "/sys", "/proc", "/dev"}


def _split_segments(command: str) -> list[str]:
    """Split a shell command into pipeline/sequence segments (best effort).

    Splits on |, ;, &&, || while respecting single/double quotes.
    Used only for first-word checks; the full command still goes
    through the regex patterns and path checks below.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in ("|", ";"):
            segments.append("".join(current))
            current = []
            # swallow a second | or & (||, &&)
            if i + 1 < len(command) and command[i + 1] in ("|", "&"):
                i += 1
        elif ch == "&":
            segments.append("".join(current))
            current = []
            if i + 1 < len(command) and command[i + 1] == "&":
                i += 1
        elif ch == "\n":
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current))
    return [s.strip() for s in segments if s.strip()]


def _first_word(segment: str) -> str:
    """First word of a segment, skipping VAR=val assignments and `env`."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return ""
    for tok in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            continue  # environment assignment, e.g. FOO=1 cmd
        if tok == "env":
            continue
        if tok == "command":
            continue
        return os.path.basename(tok)
    return ""


def check_command_blocked(
    command: str,
    workspace: str | Path,
    *,
    allow_systemctl_manage: bool = False,
    extra_patterns: list[str] | None = None,
) -> str | None:
    """Return a refusal reason if ``command`` must not run, else None.

    Fail closed: unparseable commands are refused.
    """
    if not command or not command.strip():
        return "empty command"
    if "\x00" in command:
        return "NUL byte in command"
    if len(command) > 8000:
        return "command too long (max 8000 chars)"
    if "\n" in command.strip() and command.strip().count("\n") > 5:
        return "multi-line commands over 5 lines are refused in v0.1"

    # 1. Fixed regex patterns (checked on the raw string).
    for pattern, reason in BANNED_PATTERNS:
        if pattern.search(command):
            # rm -rf needs a second look: "rm -rf ./build" inside the
            # workspace is still refused in v0.1 (too easy to fat-finger
            # into disaster). Baji deletes big trees himself.
            return f"blocked: {reason}"
    for raw in extra_patterns or []:
        if re.search(raw, command):
            return f"blocked by configured extra pattern: {raw}"

    # 2. shlex must parse cleanly, otherwise refuse (fail closed).
    try:
        shlex.split(command, posix=True)
    except ValueError as exc:
        return f"blocked: unparseable shell quoting ({exc})"

    # 3. First-word checks per pipeline segment.
    for segment in _split_segments(command):
        word = _first_word(segment)
        if not word:
            return "blocked: could not determine command word"
        if word in BANNED_FIRST_WORDS:
            return f"blocked: command {word!r} is never allowed via the daemon"
        if word == "systemctl" and not allow_systemctl_manage:
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                return "blocked: unparseable systemctl invocation"
            if any(t in SYSTEMCTL_MANAGING_VERBS for t in tokens):
                return ("blocked: systemctl management verbs need explicit "
                        "opt-in (allow_systemctl_manage); read-only "
                        "status/is-active are allowed")
        if word == "rm":
            # Any recursive rm is refused in v0.1, even inside workspace.
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                return "blocked: unparseable rm invocation"
            flags = "".join(t[1:] for t in tokens[1:] if t.startswith("-") and len(t) > 1)
            if "r" in flags or "R" in flags:
                return "blocked: recursive rm is refused via the daemon"
        if word == "rsync":
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                return "blocked: unparseable rsync invocation"
            for tok in tokens[1:]:
                # user@host:path or host:path means a remote transfer.
                if re.match(r"^[^/\s]+@[^/\s:]+:", tok) or re.match(
                    r"^[^/\s:]+:[^/\s]", tok
                ):
                    return "blocked: remote rsync destinations are refused"
        if word in ("chmod", "chown", "chgrp"):
            # chown/chgrp already banned; chmod to 777 caught by regex.
            # chmod on anything outside the workspace is caught by the
            # path check below.
            pass

    # 4. Path check: every path-like argument must stay in the workspace.
    #    Tokens starting with /, ~, or ../ are treated as paths. A small
    #    allowlist covers harmless device/stdin conventions.
    path_allowlist = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"}
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "blocked: unparseable command"
    root = resolve_workspace(workspace)
    for tok in tokens:
        # Strip redirect prefixes glued to a path, e.g. ">/tmp/x".
        probe = tok.lstrip("><")
        if probe in path_allowlist:
            continue
        is_path_like = (
            probe.startswith("/")
            or probe.startswith("~/")
            or probe == "~"
            or probe == ".."
            or probe.startswith("../")
            or "/../" in probe
            or probe.endswith("/..")
            or probe.startswith("$HOME")
        )
        if not is_path_like:
            continue
        if probe in NEVER_PATHS:
            return f"blocked: path {probe!r} is never allowed"
        # Expand $HOME / ~ for the containment check only.
        expanded = probe.replace("$HOME", str(Path.home()), 1)
        try:
            resolved = resolve_inside(root, expanded)
        except ValueError:
            return f"blocked: path escapes workspace: {probe!r}"
        if resolved == root and _first_word(command) in ("rm",):
            return "blocked: refusing to operate on the workspace root itself"
        del resolved

    # 5. Redirect targets: ">" / ">>" followed by a path outside workspace.
    redirect_match = re.findall(r">{1,2}\s*([^\s;|&]+)", command)
    for target in redirect_match:
        if target in ("&1", "&2", "/dev/null"):
            continue
        try:
            resolve_inside(root, target.strip("'\""))
        except ValueError:
            return f"blocked: redirect target escapes workspace: {target!r}"

    return None
