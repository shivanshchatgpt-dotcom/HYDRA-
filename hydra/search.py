"""Workspace code search: ripgrep when present, grep as fallback.

The pattern is NEVER run through a shell: both backends are invoked
with argv lists, and the search root is always a jail-resolved path
inside the workspace (or a configured read-only path). Search is
read-only; it cannot modify files.

Backend differences (documented, not hidden):
  * ripgrep respects .gitignore and skips hidden files by default;
    grep does not (it only skips .git and binary files).
  * ripgrep --glob matches relative paths (e.g. "src/**/*.py");
    grep --include matches basenames only (e.g. "*.py").
  * ripgrep reports a column; grep does not (column is null).

NO model code, NO network calls. The pattern itself is never logged
by the daemon (only its length/hash, per the audit rules).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

MAX_PATTERN_CHARS = 2000
MAX_RESULTS = 500
DEFAULT_MAX_RESULTS = 100
MAX_LINE_CHARS = 2000
SEARCH_TIMEOUT_SECONDS = 30


class SearchError(RuntimeError):
    """User-facing search failure (bad pattern, missing tool, ...)."""


def _validate_glob(glob: str | None) -> str | None:
    if glob is None:
        return None
    if not isinstance(glob, str) or not glob.strip():
        raise SearchError("glob must be a non-empty string")
    if "\x00" in glob or glob.startswith("/") or ".." in glob.split("/"):
        raise SearchError("glob must be a relative pattern without '..'")
    return glob


def _truncate_line(text: str) -> str:
    if len(text) > MAX_LINE_CHARS:
        return text[:MAX_LINE_CHARS]
    return text


def run_search(
    pattern: str,
    root: Path,
    *,
    glob: str | None = None,
    fixed_strings: bool = False,
    case_sensitive: bool = True,
    max_results: int = DEFAULT_MAX_RESULTS,
    backend: str | None = None,
) -> dict:
    """Search ``pattern`` under jail-resolved ``root``.

    ``backend`` is "ripgrep", "grep", or None for auto-detect (used by
    tests to force the fallback). Returns a result dict; raises
    SearchError on bad input, bad regex, timeout, or missing tools.
    """
    if not isinstance(pattern, str) or not pattern:
        raise SearchError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise SearchError("pattern too long")
    glob = _validate_glob(glob)
    max_results = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), MAX_RESULTS))

    if backend is None:
        backend = "ripgrep" if shutil.which("rg") else "grep"
    if backend == "ripgrep" and not shutil.which("rg"):
        raise SearchError("ripgrep (rg) is not installed")
    if backend == "grep" and not shutil.which("grep"):
        raise SearchError("grep is not installed")
    if backend not in ("ripgrep", "grep"):
        raise SearchError(f"unknown search backend: {backend!r}")

    if root.is_file():
        search_dir = root.parent
        target = root.name
    else:
        search_dir = root
        target = "."

    if backend == "ripgrep":
        matches, truncated = _run_ripgrep(
            pattern, search_dir, target, glob, fixed_strings,
            case_sensitive, max_results)
    else:
        matches, truncated = _run_grep(
            pattern, search_dir, target, glob, fixed_strings,
            case_sensitive, max_results)
    return {
        "pattern": pattern,
        "backend": backend,
        "matches": matches,
        "match_count": len(matches),
        "truncated": truncated,
    }


def _run_ripgrep(
    pattern: str, search_dir: Path, target: str, glob: str | None,
    fixed_strings: bool, case_sensitive: bool, max_results: int,
) -> tuple[list[dict], bool]:
    argv = ["rg", "--json", "--no-messages"]
    if fixed_strings:
        argv.append("--fixed-strings")
    if not case_sensitive:
        argv.append("--ignore-case")
    if glob:
        argv += ["--glob", glob]
    argv += ["--", pattern, target]
    try:
        proc = subprocess.run(
            argv, cwd=str(search_dir), capture_output=True, text=True,
            timeout=SEARCH_TIMEOUT_SECONDS, errors="replace")
    except subprocess.TimeoutExpired:
        raise SearchError("search timed out") from None
    if proc.returncode == 2:
        detail = (proc.stderr or "invalid pattern").strip()[:300]
        raise SearchError(f"ripgrep error: {detail}")
    matches: list[dict] = []
    truncated = False
    for line in proc.stdout.splitlines():
        if len(matches) > max_results:
            truncated = True
            break
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match":
            continue
        data = record.get("data", {})
        path_text = data.get("path", {}).get("text", "")
        line_text = data.get("lines", {}).get("text", "").rstrip("\n")
        submatches = data.get("submatches", [])
        column = submatches[0]["start"] + 1 if submatches else None
        matches.append({
            "path": path_text,
            "line": int(data.get("line_number", 0)),
            "column": column,
            "text": _truncate_line(line_text),
        })
    if len(matches) > max_results:
        matches = matches[:max_results]
        truncated = True
    return matches, truncated


def _run_grep(
    pattern: str, search_dir: Path, target: str, glob: str | None,
    fixed_strings: bool, case_sensitive: bool, max_results: int,
) -> tuple[list[dict], bool]:
    # -H: always print the filename (grep omits it for a single-file
    # target, which would break the path:line:text parsing below).
    argv = ["grep", "-r", "-n", "-H", "-I", "--no-messages",
            "--exclude-dir=.git"]
    argv.append("-F" if fixed_strings else "-E")
    if not case_sensitive:
        argv.append("-i")
    if glob:
        argv.append(f"--include={glob}")
    argv += ["--", pattern, target]
    try:
        proc = subprocess.run(
            argv, cwd=str(search_dir), capture_output=True, text=True,
            timeout=SEARCH_TIMEOUT_SECONDS, errors="replace")
    except subprocess.TimeoutExpired:
        raise SearchError("search timed out") from None
    if proc.returncode == 2:
        detail = (proc.stderr or "invalid pattern").strip()[:300]
        raise SearchError(f"grep error: {detail}")
    # returncode 1 means "no matches" — an empty result, not an error.
    matches: list[dict] = []
    truncated = False
    for line in proc.stdout.splitlines():
        if len(matches) > max_results:
            truncated = True
            break
        # Format: path:line:text (path may itself contain ':').
        first = line.find(":")
        second = line.find(":", first + 1) if first != -1 else -1
        if first == -1 or second == -1:
            continue
        try:
            lineno = int(line[first + 1:second])
        except ValueError:
            continue
        matches.append({
            "path": line[:first],
            "line": lineno,
            "column": None,
            "text": _truncate_line(line[second + 1:]),
        })
    if len(matches) > max_results:
        matches = matches[:max_results]
        truncated = True
    return matches, truncated
