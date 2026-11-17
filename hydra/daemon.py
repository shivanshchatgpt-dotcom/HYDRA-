"""HYDRA daemon v0.4: a DUMB executor for Baji's laptop.

NO model. NO Ollama. NO LLM API calls. NO internet calls.
Mikey (in the cloud) is the brain. This daemon is only the body:
it runs exactly the file/shell/memory operations Mikey asks for,
inside a workspace jail, with a command blocklist, and logs them.

Transport: this daemon only speaks HTTP+JSON on localhost or on an
explicitly configured Tailscale IP. Tailscale provides the private
network; the daemon never exposes itself to the public internet
unless Baji passes --allow-public (never do that without understanding
the risk: anyone who reaches it + the token controls the workspace).

Stdlib only. Python 3.11+.

Run:
    python3 -m hydra --help
"""

from __future__ import annotations

import argparse
import atexit
import base64
import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .audit import AuditLog
from .browser import (
    DEFAULT_CDP_HOST,
    DEFAULT_CDP_PORT,
    BrowserUnavailableError,
    CdpError,
    browser_click,
    browser_content,
    browser_evaluate,
    browser_navigate,
    browser_shot,
    browser_status,
    browser_type,
    validate_loopback,
)
from .doctor import run_doctor
from .gui import (
    GuiInputError,
    GuiUnavailableError,
    input_click,
    input_move,
    input_type,
    take_screenshot,
)
from .jobs import JobManager
from .memory_store import MemoryStore
from .sandbox import SandboxUnavailableError, build_bwrap_argv
from .search import SearchError, run_search
from .security import (
    check_command_blocked,
    load_token,
    resolve_inside,
    resolve_readable,
    resolve_workspace,
    sha256_prefix,
    token_matches,
)
from .sysinfo import GitUnavailableError, git_status, system_info
from .web import (
    WebBlockedError,
    WebError,
    WebFetchError,
    fetch as web_fetch,
    search as web_search,
    text_fingerprint,
    url_fingerprint,
)
from .tailscale import (
    TailscaleError,
    TailscaleUnavailableError,
    resolve_tailscale_ip,
    tailscale_status,
    tailscale_summary,
)
from .video import (
    DiskSpaceError,
    VideoError,
    VideoUnavailableError,
    check_disk_space,
    create_project,
    estimate_output_bytes,
    extract_frames,
    list_projects,
    load_project,
    probe_media,
    video_tools,
)
from .workflows import WorkflowError, WorkflowManager

VERSION = "0.5.0"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
MAX_JOB_TIMEOUT_SECONDS = 86400  # jobs may run long; default is 1 hour
DEFAULT_JOB_TIMEOUT_SECONDS = 3600
MAX_OUTPUT_BYTES = 200 * 1024  # per stream (stdout / stderr)
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_LIST_ENTRIES = 500


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    """Precedence: CLI flags > HYDRA_* env vars > JSON config > defaults."""

    def __init__(self, args: argparse.Namespace):
        home = Path.home()
        cfg: dict = {}
        config_path = (
            os.environ.get("HYDRA_CONFIG", "") or getattr(args, "config", "")
        )
        if config_path:
            cfg = json.loads(Path(config_path).expanduser().read_text())

        def pick(cli_value, env_name: str, cfg_key: str, default):
            if cli_value not in (None, ""):
                return cli_value
            env_value = os.environ.get(env_name, "")
            if env_value:
                return env_value
            if cfg.get(cfg_key) not in (None, ""):
                return cfg[cfg_key]
            return default

        data_dir = Path(pick(
            getattr(args, "data_dir", None), "HYDRA_DATA_DIR", "data_dir",
            str(home / ".local" / "share" / "hydra"))).expanduser()
        self.data_dir = data_dir
        self.bind = str(pick(
            args.bind, "HYDRA_BIND", "bind", DEFAULT_BIND))
        self.port = int(pick(
            args.port, "HYDRA_PORT", "port", DEFAULT_PORT))
        # Bind mode "tailscale": resolve the current Tailscale IPv4
        # via the local `tailscale ip -4` CLI (no sudo, no network
        # call from the daemon). Fails closed: if Tailscale is down
        # or returns no usable IP, startup refuses (see main()).
        # The resolved IP still goes through validate_bind below,
        # so only a 100.64.0.0/10 address is ever bound. Only the
        # resolved IP is ever logged — never the token.
        self.bind_raw = self.bind
        self.bind_mode = "explicit"
        if self.bind.strip().lower() == "tailscale":
            self.bind = resolve_tailscale_ip()
            self.bind_mode = "tailscale"
        self.workspace = Path(pick(
            args.workspace, "HYDRA_WORKSPACE", "workspace",
            str(home / "hydra-workspace"))).expanduser()
        self.token_file = Path(pick(
            args.token_file, "HYDRA_TOKEN_FILE", "token_file",
            str(data_dir / "token"))).expanduser()
        self.allow_public = bool(args.allow_public)
        self.allow_systemctl_manage = bool(args.allow_systemctl_manage) or bool(
            cfg.get("allow_systemctl_manage", False))
        self.read_only_paths: list[str] = [
            str(p) for p in cfg.get("read_only_paths", [])]
        self.extra_patterns: list[str] = [
            str(p) for p in cfg.get("blocklist_extra", [])]

        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.data_dir / "audit.jsonl"
        self.db_path = self.data_dir / "memory.db"


def validate_bind(bind: str, allow_public: bool) -> None:
    """Refuse unsafe binds. Localhost + Tailscale (100.64/10) only.

    Raises ValueError when the bind address is not allowed. Passing
    --allow-public explicitly permits 0.0.0.0 (loud warning at startup).
    """
    if bind in ("localhost", "127.0.0.1", "::1"):
        return
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        raise ValueError(f"bind must be an IP address, got {bind!r}") from None
    if ip.is_loopback:
        return
    tailscale_net = ipaddress.ip_network("100.64.0.0/10")
    if ip in tailscale_net:
        return
    if bind == "0.0.0.0" and allow_public:
        return
    raise ValueError(
        f"refusing to bind {bind}: only 127.0.0.1 or a Tailscale IP "
        "(100.64.0.0/10) is allowed without --allow-public"
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class HydraHandler(BaseHTTPRequestHandler):
    # Set by make_server(); shared across handler instances.
    config: Config
    token: str
    audit: AuditLog
    store: MemoryStore
    jobs: JobManager
    workflows: WorkflowManager
    server: ThreadingHTTPServer  # type: ignore[assignment]

    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # keep stdout clean
        sys.stderr.write(f"[hydra] {self.address_string()} {fmt % args}\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _drain_body(self) -> None:
        """Discard an unread request body so HTTP/1.1 keep-alive stays sane.

        Without this, a 401 sent before reading the body leaves the body
        bytes in the socket, and the server misparses them as the next
        request. Oversized bodies just close the connection instead.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            return
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                self.close_connection = True

    def _require_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._drain_body()
            self._send_json(401, {"ok": False, "error": "missing bearer token"})
            return False
        if not token_matches(auth[len("Bearer "):].strip(), self.token):
            self._drain_body()
            self._send_json(401, {"ok": False, "error": "invalid token"})
            return False
        return True

    def _audit(self, endpoint: str, ok: bool, **details) -> None:
        # Client IP only. Headers (and therefore the token) are never logged.
        self.audit.record(endpoint, self.address_string(), ok, **details)

    # -- routes ----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - http.server naming
        path = urlparse(self.path).path
        if path == "/health":
            # Minimal + unauthenticated on purpose: proves the daemon is
            # alive without revealing paths, config, or anything else.
            # v0.3 adds uptime + job counts (no job contents, no paths).
            started = getattr(self, "started_at", None)
            uptime = (round(time.monotonic() - started, 1)
                      if started is not None else None)
            counts = self.jobs.counts()
            self._send_json(200, {
                "ok": True, "service": "hydra-daemon", "version": VERSION,
                "uptime_seconds": uptime,
                "jobs_running": counts["running"],
                "jobs_finished": counts["finished"],
                # v0.4: video tool presence booleans (no paths, no
                # versions — presence only, same minimal spirit).
                "video_tools": video_tools(),
                # v0.4 (Tailscale-auto): Tailscale presence /
                # connection / IP only. No keys, no peer data, no
                # secrets; the probe never fails the health check.
                "tailscale": tailscale_summary(),
            })
            return
        self._send_json(404, {"ok": False, "error": "unknown endpoint"})

    def do_POST(self):  # noqa: N802 - http.server naming
        path = urlparse(self.path).path
        routes = {
            "/exec": self._handle_exec,
            "/read": self._handle_read,
            "/write": self._handle_write,
            "/edit": self._handle_edit,
            "/search": self._handle_search,
            "/list": self._handle_list,
            "/jobs/start": self._handle_jobs_start,
            "/jobs/poll": self._handle_jobs_poll,
            "/jobs/kill": self._handle_jobs_kill,
            "/jobs/list": self._handle_jobs_list,
            "/memory/add": self._handle_memory_add,
            "/memory/search": self._handle_memory_search,
            "/memory/delete": self._handle_memory_delete,
            "/memory/list": self._handle_memory_list,
            "/memory/graph/add": self._handle_graph_add,
            "/memory/graph/neighbors": self._handle_graph_neighbors,
            "/screen/shot": self._handle_screen_shot,
            "/input/move": self._handle_input_move,
            "/input/click": self._handle_input_click,
            "/input/type": self._handle_input_type,
            "/system/info": self._handle_system_info,
            "/system/tailscale": self._handle_system_tailscale,
            "/system/doctor": self._handle_system_doctor,
            "/web/search": self._handle_web_search,
            "/web/fetch": self._handle_web_fetch,
            "/git/status": self._handle_git_status,
            "/browser/status": self._handle_browser_status,
            "/browser/navigate": self._handle_browser_navigate,
            "/browser/evaluate": self._handle_browser_evaluate,
            "/browser/content": self._handle_browser_content,
            "/browser/shot": self._handle_browser_shot,
            "/browser/click": self._handle_browser_click,
            "/browser/type": self._handle_browser_type,
            "/video/probe": self._handle_video_probe,
            "/video/project/create": self._handle_video_project_create,
            "/video/render/start": self._handle_video_render_start,
            "/video/render/poll": self._handle_video_render_poll,
            "/video/preview": self._handle_video_preview,
            "/video/list": self._handle_video_list,
            "/shutdown": self._handle_shutdown,
        }
        handler = routes.get(path)
        if handler is None:
            self._drain_body()
            self._send_json(404, {"ok": False, "error": "unknown endpoint"})
            return
        if not self._require_auth():
            self._audit(path, False, reason="auth_failed")
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._audit(path, False, reason="bad_request")
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        handler(payload)

    # -- file + shell endpoints ------------------------------------------

    def _scrubbed_env(self) -> dict[str, str]:
        # HYDRA secrets never reach the child.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HYDRA_")}
        env.setdefault("TERM", "dumb")
        return env

    def _prepare_command(
        self, payload: dict, endpoint: str, max_timeout: int,
        default_timeout: int,
    ) -> tuple[str, Path, int, list[str], bool] | None:
        """Validate an exec-like request: jail + blocklist + sandbox.

        Returns (command, cwd, timeout, argv, sandboxed), or sends the
        error response + audit entry and returns None. Used by /exec
        and /jobs/start so both go through identical checks.
        """
        command = payload.get("command", "")
        if not isinstance(command, str):
            self._send_json(400, {"ok": False, "error": "command must be a string"})
            return None
        cwd_param = payload.get("cwd", ".")
        try:
            cwd = resolve_inside(self.config.workspace, str(cwd_param or "."))
        except ValueError as exc:
            self._audit(endpoint, False, reason="cwd_escape")
            self._send_json(403, {"ok": False, "error": str(exc)})
            return None
        try:
            timeout = int(payload.get("timeout_seconds", default_timeout))
        except (TypeError, ValueError):
            timeout = default_timeout
        timeout = max(1, min(timeout, max_timeout))

        blocked = check_command_blocked(
            command, self.config.workspace,
            allow_systemctl_manage=self.config.allow_systemctl_manage,
            extra_patterns=self.config.extra_patterns,
        )
        audit_cmd = command[:200]
        if blocked:
            self._audit(endpoint, False, reason="blocked",
                        command_preview=audit_cmd,
                        command_len=len(command))
            self._send_json(403, {
                "ok": False, "error": blocked, "blocked": True,
            })
            return None

        sandboxed = bool(payload.get("sandbox", False))
        if sandboxed:
            no_network = bool(payload.get("sandbox_no_network", True))
            try:
                argv = build_bwrap_argv(
                    command, self.config.workspace, cwd,
                    no_network=no_network)
            except SandboxUnavailableError as exc:
                self._audit(endpoint, False, reason="sandbox_unavailable",
                            command_preview=audit_cmd,
                            command_len=len(command))
                self._send_json(501, {"ok": False, "error": str(exc)})
                return None
        else:
            argv = ["/bin/sh", "-c", command]
        return command, cwd, timeout, argv, sandboxed

    def _handle_exec(self, payload: dict) -> None:
        prepared = self._prepare_command(
            payload, "/exec", MAX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        if prepared is None:
            return
        command, cwd, timeout, argv, sandboxed = prepared
        result = _run_command(argv, cwd, timeout, self._scrubbed_env())
        self._audit("/exec", True, command_preview=command[:200],
                    command_len=len(command), sandboxed=sandboxed,
                    returncode=result["returncode"],
                    timed_out=result["timed_out"],
                    stdout_len=len(result["stdout"]),
                    stderr_len=len(result["stderr"]))
        self._send_json(200, {"ok": True, "sandboxed": sandboxed, **result})

    # -- background jobs ---------------------------------------------------

    def _handle_jobs_start(self, payload: dict) -> None:
        prepared = self._prepare_command(
            payload, "/jobs/start", MAX_JOB_TIMEOUT_SECONDS,
            DEFAULT_JOB_TIMEOUT_SECONDS)
        if prepared is None:
            return
        command, cwd, timeout, argv, sandboxed = prepared
        detached = bool(payload.get("detached", False))
        try:
            job = self.jobs.start(
                argv, command, cwd, self._scrubbed_env(),
                timeout_seconds=timeout, sandboxed=sandboxed,
                detached=detached)
        except RuntimeError as exc:
            self._audit("/jobs/start", False, reason="too_many_jobs")
            self._send_json(429, {"ok": False, "error": str(exc)})
            return
        self._audit("/jobs/start", True, job_id=job.job_id,
                    command_preview=command[:200],
                    command_len=len(command), sandboxed=sandboxed,
                    detached=detached)
        self._send_json(200, {
            "ok": True, "job_id": job.job_id, "status": "running",
            "sandboxed": sandboxed, "detached": detached,
            "timeout_seconds": timeout,
        })

    def _handle_jobs_poll(self, payload: dict) -> None:
        job_id = str(payload.get("job_id", ""))
        try:
            offset = int(payload.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        job = self.jobs.get(job_id)
        if job is None:
            self._send_json(404, {"ok": False, "error": "unknown job_id"})
            return
        snapshot = job.snapshot(stdout_offset=offset)
        self._audit("/jobs/poll", True, job_id=job_id,
                    status=snapshot["status"])
        self._send_json(200, {"ok": True, **snapshot})

    def _handle_jobs_kill(self, payload: dict) -> None:
        job_id = str(payload.get("job_id", ""))
        killed, job = self.jobs.kill(job_id)
        if job is None:
            self._send_json(404, {"ok": False, "error": "unknown job_id"})
            return
        self._audit("/jobs/kill", True, job_id=job_id, killed=killed)
        self._send_json(200, {
            "ok": True, "job_id": job_id, "killed": killed,
            "status": job.status, "returncode": job.returncode,
        })

    def _handle_jobs_list(self, payload: dict) -> None:
        jobs = self.jobs.list_jobs()
        self._audit("/jobs/list", True, count=len(jobs))
        self._send_json(200, {"ok": True, "jobs": jobs, "count": len(jobs)})

    def _handle_read(self, payload: dict) -> None:
        rel = str(payload.get("path", ""))
        try:
            max_bytes = int(payload.get("max_bytes", MAX_OUTPUT_BYTES))
        except (TypeError, ValueError):
            max_bytes = MAX_OUTPUT_BYTES
        max_bytes = max(1, min(max_bytes, MAX_REQUEST_BYTES))
        start_line = payload.get("start_line")
        end_line = payload.get("end_line")
        if start_line is not None or end_line is not None:
            try:
                start_line = int(start_line if start_line is not None else 1)
                end_line = int(end_line) if end_line is not None else None
            except (TypeError, ValueError):
                self._send_json(400, {
                    "ok": False,
                    "error": "start_line/end_line must be integers"})
                return
            if start_line < 1 or (end_line is not None and end_line < start_line):
                self._send_json(400, {
                    "ok": False,
                    "error": "line range must satisfy 1 <= start_line <= end_line"})
                return
        try:
            path, _ro = resolve_readable(
                self.config.workspace, rel, self.config.read_only_paths
            )
        except ValueError as exc:
            self._audit("/read", False, reason="path_escape", path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.is_file():
            self._audit("/read", False, reason="not_found", path=rel[:256])
            self._send_json(404, {"ok": False, "error": "not a file"})
            return
        data = path.read_bytes()[: max_bytes + 1]
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        content = data.decode("utf-8", errors="replace")
        response: dict = {
            "ok": True, "path": rel, "content": content,
            "truncated": truncated, "size": path.stat().st_size,
            "encoding": "utf-8-replace",
        }
        if start_line is not None or end_line is not None:
            lines = content.split("\n")
            if content.endswith("\n"):
                lines = lines[:-1]  # trailing "" is the terminator
            total_lines = len(lines) if not truncated else None
            lo = start_line or 1
            hi = end_line if end_line is not None else len(lines)
            selected = lines[lo - 1:hi]
            ranged = "\n".join(selected)
            if selected and (hi < len(lines) or content.endswith("\n")):
                ranged += "\n"
            response.update({
                "content": ranged,
                "start_line": lo,
                "end_line": lo + len(selected) - 1 if selected else lo - 1,
                "lines_returned": len(selected),
                "total_lines": total_lines,
            })
        self._audit("/read", True, path=rel[:256], bytes_read=len(data),
                    sha256=sha256_prefix(data))
        self._send_json(200, response)

    def _handle_write(self, payload: dict) -> None:
        rel = str(payload.get("path", ""))
        content = payload.get("content", "")
        mode = str(payload.get("mode", "overwrite"))
        if not isinstance(content, str):
            self._send_json(400, {"ok": False, "error": "content must be a string"})
            return
        if mode not in ("overwrite", "append"):
            self._send_json(400, {"ok": False, "error": "mode must be overwrite|append"})
            return
        try:
            path = resolve_inside(self.config.workspace, rel)
        except ValueError as exc:
            self._audit("/write", False, reason="path_escape", path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if path.is_dir():
            self._send_json(400, {"ok": False, "error": "path is a directory"})
            return
        data = content.encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            self._send_json(400, {"ok": False, "error": "content too large"})
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if mode == "append" else "w", encoding="utf-8") as fh:
            fh.write(content)
        self._audit("/write", True, path=rel[:256],
                    bytes_written=len(data), mode=mode,
                    sha256=sha256_prefix(data))
        self._send_json(200, {
            "ok": True, "path": rel, "bytes_written": len(data), "mode": mode,
        })

    def _handle_edit(self, payload: dict) -> None:
        """Exact-string patch: replace old_string with new_string.

        Fails on zero matches (400) and on ambiguous matches (409)
        unless replace_all is true. dry_run reports what would change
        without writing. Workspace jail only (never read-only paths).
        """
        rel = str(payload.get("path", ""))
        old_string = payload.get("old_string", "")
        new_string = payload.get("new_string", "")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            self._send_json(400, {
                "ok": False,
                "error": "old_string and new_string must be strings"})
            return
        if old_string == "":
            self._send_json(400, {
                "ok": False, "error": "old_string must not be empty"})
            return
        dry_run = bool(payload.get("dry_run", False))
        replace_all = bool(payload.get("replace_all", False))
        try:
            path = resolve_inside(self.config.workspace, rel)
        except ValueError as exc:
            self._audit("/edit", False, reason="path_escape", path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.is_file():
            self._audit("/edit", False, reason="not_found", path=rel[:256])
            self._send_json(404, {"ok": False, "error": "not a file"})
            return
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {
                "ok": False,
                "error": "file is not valid UTF-8; /edit only handles text"})
            return
        matches = content.count(old_string)
        if matches == 0:
            self._audit("/edit", False, reason="no_match", path=rel[:256])
            self._send_json(400, {
                "ok": False, "error": "old_string not found in file"})
            return
        if matches > 1 and not replace_all:
            self._audit("/edit", False, reason="ambiguous",
                        path=rel[:256], matches=matches)
            self._send_json(409, {
                "ok": False,
                "error": f"ambiguous: old_string matches {matches} times; "
                         "make it unique or pass replace_all=true",
                "matches": matches,
            })
            return
        replaced = matches if replace_all else 1
        new_content = content.replace(old_string, new_string,
                                      -1 if replace_all else 1)
        # Small context preview around the first match, so the caller
        # can confirm the right spot (bounded; never the whole file).
        first = content.find(old_string)
        lo = max(0, first - 80)
        hi = min(len(content), first + len(old_string) + 80)
        preview_before = content[lo:hi]
        preview_after = new_content[lo:lo + len(preview_before)
                                    + len(new_string) - len(old_string)]
        if dry_run:
            self._audit("/edit", True, path=rel[:256], dry_run=True,
                        matches=matches)
            self._send_json(200, {
                "ok": True, "path": rel, "dry_run": True,
                "matches": matches, "would_replace": replaced,
                "bytes_before": len(raw),
                "bytes_after": len(new_content.encode("utf-8")),
                "preview_before": preview_before,
                "preview_after": preview_after,
            })
            return
        data = new_content.encode("utf-8")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_content)
        self._audit("/edit", True, path=rel[:256], matches=matches,
                    replaced=replaced, bytes_written=len(data),
                    sha256=sha256_prefix(data))
        self._send_json(200, {
            "ok": True, "path": rel, "dry_run": False,
            "matches": matches, "replaced": replaced,
            "bytes_written": len(data),
        })

    def _handle_list(self, payload: dict) -> None:
        rel = str(payload.get("path", ".") or ".")
        recursive = bool(payload.get("recursive", False))
        try:
            path, _ro = resolve_readable(
                self.config.workspace, rel, self.config.read_only_paths
            )
        except ValueError as exc:
            self._audit("/list", False, reason="path_escape", path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.exists():
            self._send_json(404, {"ok": False, "error": "path does not exist"})
            return
        entries: list[dict] = []
        if path.is_file():
            entries.append(_entry(path, path))
        else:
            iterator = path.rglob("*") if recursive else path.iterdir()
            for child in sorted(iterator, key=lambda p: str(p)):
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                entries.append(_entry(child, path))
        self._audit("/list", True, path=rel[:256],
                    entries=len(entries), recursive=recursive)
        self._send_json(200, {
            "ok": True, "path": rel, "entries": entries,
            "truncated": len(entries) >= MAX_LIST_ENTRIES,
        })

    def _handle_search(self, payload: dict) -> None:
        pattern = payload.get("pattern", "")
        if not isinstance(pattern, str):
            self._send_json(400, {"ok": False, "error": "pattern must be a string"})
            return
        rel = str(payload.get("path", ".") or ".")
        try:
            path, _ro = resolve_readable(
                self.config.workspace, rel, self.config.read_only_paths
            )
        except ValueError as exc:
            self._audit("/search", False, reason="path_escape", path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.exists():
            self._send_json(404, {"ok": False, "error": "path does not exist"})
            return
        try:
            max_results = int(payload.get("max_results", 100))
        except (TypeError, ValueError):
            max_results = 100
        try:
            result = run_search(
                pattern, path,
                glob=payload.get("glob"),
                fixed_strings=bool(payload.get("fixed_strings", False)),
                case_sensitive=bool(payload.get("case_sensitive", True)),
                max_results=max_results,
            )
        except SearchError as exc:
            self._audit("/search", False, reason="search_error",
                        pattern_len=len(pattern))
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/search", True, path=rel[:256],
                    pattern_len=len(pattern),
                    sha256=sha256_prefix(pattern.encode("utf-8")),
                    backend=result["backend"],
                    match_count=result["match_count"])
        self._send_json(200, {"ok": True, **result})

    # -- memory endpoints --------------------------------------------------

    def _handle_memory_add(self, payload: dict) -> None:
        text = payload.get("text", "")
        if not isinstance(text, str):
            self._send_json(400, {"ok": False, "error": "text must be a string"})
            return
        try:
            memory_id = self.store.add_memory(
                text,
                kind=str(payload.get("kind", "note")),
                source=str(payload.get("source", "")),
                entities=payload.get("entities") or [],
                relations=payload.get("relations") or [],
            )
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/memory/add", True, id=memory_id,
                    text_len=len(text),
                    sha256=sha256_prefix(text.encode("utf-8")))
        self._send_json(200, {"ok": True, "id": memory_id})

    def _handle_memory_search(self, payload: dict) -> None:
        query = str(payload.get("query", ""))
        try:
            limit = int(payload.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        result = self.store.search(query, limit=limit)
        self._audit("/memory/search", True, query_len=len(query),
                    hits=len(result["results"]),
                    fts_available=result["fts_available"])
        self._send_json(200, {"ok": True, **result})

    def _handle_graph_add(self, payload: dict) -> None:
        try:
            rel_id = self.store.add_relation(
                str(payload.get("subject", "")),
                str(payload.get("predicate", "")),
                str(payload.get("object", "")),
                source_memory_id=payload.get("source_memory_id"),
            )
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/memory/graph/add", True,
                    subject=str(payload.get("subject", ""))[:128],
                    predicate=str(payload.get("predicate", ""))[:128],
                    object=str(payload.get("object", ""))[:128])
        self._send_json(200, {"ok": True, "id": rel_id})

    def _handle_graph_neighbors(self, payload: dict) -> None:
        entity = str(payload.get("entity", ""))
        try:
            limit = int(payload.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            neighbors = self.store.neighbors(entity, limit=limit)
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/memory/graph/neighbors", True,
                    entity=entity[:128], neighbors=len(neighbors))
        self._send_json(200, {
            "ok": True, "entity": entity, "neighbors": neighbors,
        })

    def _handle_memory_delete(self, payload: dict) -> None:
        if "id" in payload:
            try:
                memory_id = int(payload["id"])
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "id must be an integer"})
                return
            deleted = self.store.delete_memory(memory_id)
            if not deleted:
                self._send_json(404, {"ok": False, "error": "memory not found"})
                return
            self._audit("/memory/delete", True, id=memory_id)
            self._send_json(200, {"ok": True, "deleted": True, "id": memory_id})
            return
        if "relation_id" in payload:
            try:
                relation_id = int(payload["relation_id"])
            except (TypeError, ValueError):
                self._send_json(400, {
                    "ok": False, "error": "relation_id must be an integer"})
                return
            deleted = self.store.delete_relation(relation_id)
            if not deleted:
                self._send_json(404, {"ok": False, "error": "relation not found"})
                return
            self._audit("/memory/delete", True, relation_id=relation_id)
            self._send_json(200, {
                "ok": True, "deleted": True, "relation_id": relation_id})
            return
        self._send_json(400, {
            "ok": False, "error": "pass id (memory) or relation_id"})
        return

    def _handle_memory_list(self, payload: dict) -> None:
        try:
            limit = int(payload.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        kind = payload.get("kind")
        result = self.store.list_recent(
            limit=limit, kind=str(kind) if kind else None)
        self._audit("/memory/list", True, count=result["count"],
                    total=result["total"])
        self._send_json(200, {"ok": True, **result})

    # -- GUI endpoints (best effort, Wayland/KDE) --------------------------

    def _handle_screen_shot(self, payload: dict) -> None:
        try:
            result = take_screenshot()
        except GuiUnavailableError as exc:
            self._audit("/screen/shot", False, reason="gui_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        png = result["png_bytes"]
        save_to = payload.get("save_to")
        if save_to is not None:
            try:
                path = resolve_inside(self.config.workspace, str(save_to))
            except ValueError as exc:
                self._audit("/screen/shot", False, reason="path_escape")
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            self._audit("/screen/shot", True, backend=result["backend"],
                        bytes=len(png), path=str(save_to)[:256])
            self._send_json(200, {
                "ok": True, "path": str(save_to),
                "backend": result["backend"], "width": result["width"],
                "height": result["height"], "bytes": len(png),
            })
            return
        self._audit("/screen/shot", True, backend=result["backend"],
                    bytes=len(png))
        self._send_json(200, {
            "ok": True, "format": "png",
            "data_base64": base64.b64encode(png).decode("ascii"),
            "backend": result["backend"], "width": result["width"],
            "height": result["height"], "bytes": len(png),
        })

    def _handle_input_move(self, payload: dict) -> None:
        try:
            result = input_move(payload.get("x"), payload.get("y"))
        except GuiInputError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except GuiUnavailableError as exc:
            self._audit("/input/move", False, reason="gui_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        self._audit("/input/move", True, x=result["x"], y=result["y"])
        self._send_json(200, result)

    def _handle_input_click(self, payload: dict) -> None:
        try:
            result = input_click(
                payload.get("x"), payload.get("y"),
                button=str(payload.get("button", "left")),
                clicks=payload.get("clicks", 1))
        except GuiInputError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except GuiUnavailableError as exc:
            self._audit("/input/click", False, reason="gui_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        self._audit("/input/click", True, button=result["button"],
                    clicks=result["clicks"])
        self._send_json(200, result)

    def _handle_input_type(self, payload: dict) -> None:
        text = payload.get("text", "")
        try:
            result = input_type(text)
        except GuiInputError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except GuiUnavailableError as exc:
            self._audit("/input/type", False, reason="gui_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        # Never log typed text: length + hash only.
        self._audit("/input/type", True, text_len=len(text),
                    sha256=sha256_prefix(str(text).encode("utf-8")))
        self._send_json(200, result)

    # -- read-only system + git endpoints ----------------------------------

    def _handle_system_info(self, payload: dict) -> None:
        result = system_info(self.config.workspace)
        self._audit("/system/info", True)
        self._send_json(200, {"ok": True, **result})

    def _handle_system_tailscale(self, payload: dict) -> None:
        """Read-only Tailscale status (secret-free summary only).

        501 when the tailscale binary is missing; 502 when
        tailscaled is down or its output is unusable; 200 with
        connected=false when Tailscale is installed but not
        connected (e.g. NeedsLogin). No keys or peer records are
        ever returned — see hydra/tailscale.py.
        """
        try:
            result = tailscale_status()
        except TailscaleUnavailableError as exc:
            self._audit("/system/tailscale", False,
                        reason="tailscale_missing")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        except TailscaleError as exc:
            self._audit("/system/tailscale", False,
                        reason="tailscale_down")
            self._send_json(502, {"ok": False, "error": str(exc)})
            return
        self._audit("/system/tailscale", True,
                    connected=result["connected"],
                    peer_count=result["peer_count"])
        self._send_json(200, {"ok": True, **result})

    def _handle_system_doctor(self, payload: dict) -> None:
        """Read-only readiness report. The doctor never fixes
        anything and never reads the token file's contents; fix
        hints are commands the USER runs by hand."""
        result = run_doctor(
            workspace=self.config.workspace,
            data_dir=getattr(self.config, "data_dir", None),
            token_file=getattr(self.config, "token_file", None),
            bind=self.config.bind,
            port=self.config.port,
            bind_mode=getattr(self.config, "bind_mode", "explicit"),
        )
        self._audit("/system/doctor", True,
                    overall=result["overall"],
                    summary=result["summary"])
        self._send_json(200, {"ok": True, **result})

    def _handle_web_search(self, payload: dict) -> None:
        query = payload.get("query", "")
        if not isinstance(query, str) or not query.strip():
            self._audit("/web/search", False, reason="bad_request")
            self._send_json(400, {
                "ok": False, "error": "query must be a non-empty string"})
            return
        try:
            count = int(payload.get("count", 5))
        except (TypeError, ValueError):
            count = 5
        fp = text_fingerprint(query)
        try:
            results = web_search(query, count)
        except WebError as exc:
            self._audit("/web/search", False, reason="failed",
                        query=fp, error=str(exc)[:200])
            self._send_json(502, {"ok": False, "error": str(exc)})
            return
        self._audit("/web/search", True, query=fp,
                    result_count=len(results))
        self._send_json(200, {
            "ok": True, "results": results, "count": len(results)})

    def _handle_web_fetch(self, payload: dict) -> None:
        url = payload.get("url", "")
        try:
            result = web_fetch(url, payload.get("max_bytes", 200_000))
        except WebBlockedError as exc:
            self._audit("/web/fetch", False, reason="blocked")
            self._send_json(403, {"ok": False, "error": str(exc),
                                  "blocked": True})
            return
        except WebFetchError as exc:
            self._audit("/web/fetch", False, reason="failed",
                        error=str(exc)[:200])
            self._send_json(502, {"ok": False, "error": str(exc)})
            return
        except WebError as exc:
            self._audit("/web/fetch", False, reason="failed")
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/web/fetch", True, url=url_fingerprint(result["url"]),
                    status=result["status"], bytes=result["bytes"],
                    truncated=result["truncated"])
        self._send_json(200, {"ok": True, **result})

    def _handle_git_status(self, payload: dict) -> None:
        rel = str(payload.get("path", ".") or ".")
        try:
            result = git_status(self.config.workspace, rel)
        except ValueError as exc:
            self._audit("/git/status", False, reason="path_escape")
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
            return
        except GitUnavailableError as exc:
            self._audit("/git/status", False, reason="git_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        # Audit counts only: the changes list names workspace files and
        # stays in the response, not the log.
        self._audit("/git/status", True, path=rel[:256],
                    is_repo=result["is_repo"],
                    change_count=result["change_count"])
        self._send_json(200, {"ok": True, **result})

    # -- CDP browser endpoints (loopback only) -----------------------------

    def _browser_target(self, payload: dict, endpoint: str):
        """Parse + validate the CDP target from the request.

        Returns (host, port, target_id), or sends the error response
        and returns None. Non-loopback hosts are refused here, before
        any socket is opened.
        """
        host = str(payload.get("host", DEFAULT_CDP_HOST) or DEFAULT_CDP_HOST)
        try:
            port = int(payload.get("port", DEFAULT_CDP_PORT))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False,
                                  "error": "port must be an integer"})
            return None
        if not 1 <= port <= 65535:
            self._send_json(400, {"ok": False, "error": "port out of range"})
            return None
        target_id = payload.get("target_id")
        if target_id is not None and not isinstance(target_id, str):
            self._send_json(400, {"ok": False,
                                  "error": "target_id must be a string"})
            return None
        try:
            validate_loopback(host)
        except ValueError as exc:
            self._audit(endpoint, False, reason="non_loopback_cdp",
                        host=host[:64])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return None
        return host, port, target_id

    def _browser_error(self, endpoint: str, exc: Exception) -> None:
        """Map a browser-layer failure to an HTTP response.

        501 = Chrome is not running with remote debugging (tells the
        caller exactly what to start). 502 = Chrome answered but the
        CDP command failed. 400 = bad request (bad URL/expression).
        """
        if isinstance(exc, BrowserUnavailableError):
            self._audit(endpoint, False, reason="browser_unavailable")
            self._send_json(501, {"ok": False, "error": str(exc)})
        elif isinstance(exc, CdpError):
            self._audit(endpoint, False, reason="cdp_error")
            self._send_json(502, {"ok": False, "error": str(exc)})
        else:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _handle_browser_status(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/status")
        if target is None:
            return
        host, port, _target_id = target
        # A probe by design: an absent browser is a 200 with
        # available=false, not an error — the caller asked "is it up?".
        result = browser_status(host, port)
        self._audit("/browser/status", True,
                    available=result["available"])
        self._send_json(200, {"ok": True, **result})

    def _handle_browser_navigate(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/navigate")
        if target is None:
            return
        host, port, target_id = target
        url = payload.get("url", "")
        if not isinstance(url, str):
            self._send_json(400, {"ok": False, "error": "url must be a string"})
            return
        try:
            result = browser_navigate(host, port, url, target_id)
        except (ValueError, CdpError) as exc:
            self._browser_error("/browser/navigate", exc)
            return
        # Audit length + hash only, never the URL.
        self._audit("/browser/navigate", True, url_len=len(url),
                    sha256=sha256_prefix(url.encode("utf-8")),
                    target_id=result.get("target_id"))
        self._send_json(200, {"ok": True, **result})

    def _handle_browser_evaluate(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/evaluate")
        if target is None:
            return
        host, port, target_id = target
        expression = payload.get("expression", "")
        if not isinstance(expression, str):
            self._send_json(400, {"ok": False,
                                  "error": "expression must be a string"})
            return
        try:
            result = browser_evaluate(host, port, expression, target_id)
        except (ValueError, CdpError) as exc:
            self._browser_error("/browser/evaluate", exc)
            return
        self._audit("/browser/evaluate", True,
                    expression_len=len(expression),
                    sha256=sha256_prefix(expression.encode("utf-8")),
                    truncated=result.get("truncated", False))
        self._send_json(200, {"ok": True, **result})

    def _handle_browser_content(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/content")
        if target is None:
            return
        host, port, target_id = target
        try:
            result = browser_content(host, port, target_id)
        except CdpError as exc:
            self._browser_error("/browser/content", exc)
            return
        # Never log page content: lengths only.
        self._audit("/browser/content", True,
                    text_len=len(result["text"]),
                    html_len=len(result["html"]),
                    text_truncated=result["text_truncated"],
                    html_truncated=result["html_truncated"])
        self._send_json(200, {"ok": True, **result})

    def _handle_browser_shot(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/shot")
        if target is None:
            return
        host, port, target_id = target
        try:
            result = browser_shot(host, port, target_id)
        except CdpError as exc:
            self._browser_error("/browser/shot", exc)
            return
        png = result["png"]
        save_to = payload.get("save_to")
        if save_to is not None:
            try:
                path = resolve_inside(self.config.workspace, str(save_to))
            except ValueError as exc:
                self._audit("/browser/shot", False, reason="path_escape")
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            self._audit("/browser/shot", True, backend="cdp",
                        bytes=len(png), path=str(save_to)[:256])
            self._send_json(200, {
                "ok": True, "path": str(save_to), "backend": "cdp",
                "width": result["width"], "height": result["height"],
                "bytes": len(png),
            })
            return
        self._audit("/browser/shot", True, backend="cdp", bytes=len(png))
        self._send_json(200, {
            "ok": True, "format": "png",
            "data_base64": base64.b64encode(png).decode("ascii"),
            "backend": "cdp", "width": result["width"],
            "height": result["height"], "bytes": len(png),
        })

    def _handle_browser_click(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/click")
        if target is None:
            return
        host, port, target_id = target
        try:
            result = browser_click(host, port, payload.get("x"),
                                   payload.get("y"), target_id)
        except (ValueError, CdpError) as exc:
            self._browser_error("/browser/click", exc)
            return
        self._audit("/browser/click", True, x=result["x"], y=result["y"])
        self._send_json(200, {"ok": True, **result})

    def _handle_browser_type(self, payload: dict) -> None:
        target = self._browser_target(payload, "/browser/type")
        if target is None:
            return
        host, port, target_id = target
        text = payload.get("text", "")
        try:
            result = browser_type(host, port, text, target_id)
        except (ValueError, CdpError) as exc:
            self._browser_error("/browser/type", exc)
            return
        # Never log typed text: length + hash only.
        self._audit("/browser/type", True, text_len=len(text),
                    sha256=sha256_prefix(str(text).encode("utf-8")))
        self._send_json(200, {"ok": True, **result})

    # -- video endpoints (v0.4, headless MLT/melt) -------------------------

    def _rel_to_workspace(self, abs_path: str) -> str:
        """Workspace-relative form of an absolute path (for responses
        that must not leak absolute paths). Falls back to the name."""
        try:
            root = resolve_inside(self.config.workspace, ".")
            return str(Path(abs_path).relative_to(root))
        except ValueError:
            return Path(abs_path).name

    def _handle_video_probe(self, payload: dict) -> None:
        rel = str(payload.get("path", ""))
        try:
            path = resolve_inside(self.config.workspace, rel)
        except ValueError as exc:
            self._audit("/video/probe", False, reason="path_escape",
                        path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.is_file():
            self._audit("/video/probe", False, reason="not_found",
                        path=rel[:256])
            self._send_json(404, {"ok": False, "error": "not a file"})
            return
        try:
            probe = probe_media(path)
        except VideoUnavailableError as exc:
            self._audit("/video/probe", False, reason="ffprobe_missing")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        except VideoError as exc:
            self._audit("/video/probe", False, reason="probe_failed",
                        path=rel[:256])
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        probe["path"] = rel
        self._audit("/video/probe", True, path=rel[:256],
                    has_video=probe["has_video"],
                    has_audio=probe["has_audio"],
                    duration=probe["duration"])
        self._send_json(200, {"ok": True, **probe})

    def _handle_video_project_create(self, payload: dict) -> None:
        # The payload IS the timeline spec: name/profile/tracks/
        # transitions/subtitle/assets, plus optional overwrite.
        overwrite = bool(payload.get("overwrite", False))
        try:
            result = create_project(
                self.config.workspace, payload, overwrite=overwrite)
        except FileExistsError as exc:
            self._audit("/video/project/create", False, reason="exists")
            self._send_json(409, {"ok": False, "error": str(exc)})
            return
        except VideoError as exc:
            self._audit("/video/project/create", False,
                        reason="invalid_spec")
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except ValueError as exc:  # jail escapes surface as ValueError
            self._audit("/video/project/create", False,
                        reason="path_escape")
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        self._audit("/video/project/create", True,
                    project_id=result["project_id"],
                    clip_count=result["clip_count"],
                    duration=result["duration"])
        self._send_json(200, {"ok": True, **result})

    def _handle_video_render_start(self, payload: dict) -> None:
        project_id = str(payload.get("project_id", ""))
        try:
            timeline = load_project(self.config.workspace, project_id)
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
            return
        except VideoError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        output_rel = str(payload.get("output")
                         or f"video/{project_id}/output.mp4")
        try:
            resolve_inside(self.config.workspace, output_rel)
        except ValueError as exc:
            self._audit("/video/render/start", False, reason="path_escape")
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        # Honest availability check BEFORE any workflow is created:
        # without melt there is nothing to queue, so this is a 501,
        # for dry runs and real starts alike.
        if not video_tools()["melt"]:
            self._audit("/video/render/start", False,
                        reason="melt_missing", project_id=project_id)
            self._send_json(501, {
                "ok": False,
                "error": "melt is not installed; install it with: "
                         "sudo pacman -S mlt (see docs/VIDEO_SETUP.md)",
            })
            return
        try:
            timeout = int(payload.get("timeout_seconds", 14400))
        except (TypeError, ValueError):
            timeout = 14400
        timeout = max(60, min(timeout, 86400))
        duration = float(timeline.get("duration") or 0)
        profile = str(timeline.get("profile") or "1080p30")
        needed = estimate_output_bytes(duration, profile)
        guard = check_disk_space(self.config.workspace, needed)
        if not guard["ok"]:
            self._audit("/video/render/start", False,
                        reason="disk_guard", project_id=project_id,
                        free_bytes=guard["free_bytes"],
                        needed_bytes=needed)
            self._send_json(507, {
                "ok": False,
                "error": "disk guard refused the render: free space "
                         f"{guard['free_bytes']} bytes < estimated need "
                         f"{needed} bytes; free space and retry",
                "free_bytes": guard["free_bytes"],
                "needed_bytes": needed,
            })
            return
        if bool(payload.get("dry_run", False)):
            self._audit("/video/render/start", True, dry_run=True,
                        project_id=project_id)
            self._send_json(200, {
                "ok": True, "dry_run": True, "project_id": project_id,
                "profile": profile, "duration": duration,
                "output_path": output_rel,
                "estimated_bytes": needed,
                "free_bytes": guard["free_bytes"],
                "melt_available": True,
            })
            return
        try:
            created = self.workflows.create_workflow(
                project_id, output_rel, timeout_seconds=timeout)
            started = self.workflows.start(created["workflow_id"])
        except FileNotFoundError as exc:
            self._send_json(404, {"ok": False, "error": str(exc)})
            return
        except WorkflowError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._audit("/video/render/start", True,
                    project_id=project_id,
                    workflow_id=started["workflow_id"],
                    output_path=output_rel[:256])
        self._send_json(200, {"ok": True, **started})

    def _handle_video_render_poll(self, payload: dict) -> None:
        workflow_id = str(payload.get("workflow_id", ""))
        snapshot = self.workflows.snapshot(workflow_id)
        if snapshot is None:
            self._send_json(404, {"ok": False, "error": "unknown workflow_id"})
            return
        if bool(payload.get("resume", False)):
            try:
                snapshot = self.workflows.resume(workflow_id)
            except WorkflowError as exc:
                self._audit("/video/render/poll", False,
                            reason="resume_refused",
                            workflow_id=workflow_id)
                self._send_json(409, {"ok": False, "error": str(exc)})
                return
            self._audit("/video/render/poll", True,
                        workflow_id=workflow_id, resumed=True,
                        status=snapshot["status"])
            self._send_json(200, {"ok": True, **snapshot})
            return
        self._audit("/video/render/poll", True, workflow_id=workflow_id,
                    status=snapshot["status"],
                    progress_percent=snapshot["progress_percent"])
        self._send_json(200, {"ok": True, **snapshot})

    def _handle_video_preview(self, payload: dict) -> None:
        rel = str(payload.get("path", ""))
        try:
            path = resolve_inside(self.config.workspace, rel)
        except ValueError as exc:
            self._audit("/video/preview", False, reason="path_escape",
                        path=rel[:256])
            self._send_json(403, {"ok": False, "error": str(exc)})
            return
        if not path.is_file():
            self._send_json(404, {"ok": False, "error": "not a file"})
            return
        output_dir_rel = payload.get("output_dir")
        if output_dir_rel is not None:
            try:
                output_dir = resolve_inside(
                    self.config.workspace, str(output_dir_rel))
            except ValueError as exc:
                self._audit("/video/preview", False,
                            reason="path_escape")
                self._send_json(403, {"ok": False, "error": str(exc)})
                return
        else:
            output_dir = path.parent / "previews"
        timestamps = payload.get("timestamps")
        if timestamps is None:
            try:
                probe = probe_media(path)
            except VideoUnavailableError as exc:
                self._audit("/video/preview", False,
                            reason="ffprobe_missing")
                self._send_json(501, {"ok": False, "error": str(exc)})
                return
            except VideoError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            duration = probe.get("duration")
            if duration is None:
                self._send_json(400, {
                    "ok": False,
                    "error": "cannot pick preview timestamps: media has "
                             "no duration; pass timestamps explicitly"})
                return
            timestamps = [round(float(duration) * f, 3)
                          for f in (0.1, 0.5, 0.9)]
        if not isinstance(timestamps, list) or not timestamps:
            self._send_json(400, {
                "ok": False,
                "error": "timestamps must be a non-empty list of seconds"})
            return
        try:
            frames = extract_frames(path, timestamps, output_dir)
        except VideoUnavailableError as exc:
            self._audit("/video/preview", False, reason="ffmpeg_missing")
            self._send_json(501, {"ok": False, "error": str(exc)})
            return
        except VideoError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        rel_frames = [{"timestamp": f["timestamp"],
                       "path": self._rel_to_workspace(f["path"]),
                       "bytes": f["bytes"]} for f in frames]
        self._audit("/video/preview", True, path=rel[:256],
                    frames=len(rel_frames))
        self._send_json(200, {
            "ok": True, "path": rel, "frames": rel_frames,
            "paths": [f["path"] for f in rel_frames],
        })

    def _handle_video_list(self, payload: dict) -> None:
        projects = list_projects(self.config.workspace)
        workflows = self.workflows.list_workflows()
        self._audit("/video/list", True, projects=len(projects),
                    workflows=len(workflows))
        self._send_json(200, {
            "ok": True, "projects": projects,
            "workflows": workflows,
            "render_busy": self.workflows.render_busy(),
        })

    # -- shutdown ----------------------------------------------------------

    def _handle_shutdown(self, payload: dict) -> None:
        if payload.get("confirm") is not True:
            self._send_json(400, {
                "ok": False, "error": "shutdown needs {\"confirm\": true}",
            })
            return
        killed = self.jobs.kill_all()
        interrupted = self.workflows.interrupt_all()
        self._audit("/shutdown", True, jobs_killed=killed,
                    workflows_interrupted=interrupted)
        self._send_json(200, {"ok": True, "message": "shutting down",
                              "jobs_killed": killed,
                              "workflows_interrupted": interrupted})
        # Shut down from a thread: shutdown() blocks until serve_forever
        # returns, and we are inside a request handler thread.
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def _entry(path: Path, root: Path) -> dict:
    try:
        stat = path.lstat()
    except OSError:
        return {"name": path.name, "type": "unknown"}
    if path.is_symlink():
        kind = "symlink"
    elif path.is_dir():
        kind = "dir"
    elif path.is_file():
        kind = "file"
    else:
        kind = "other"
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = path.name
    return {"name": rel, "type": kind, "size": stat.st_size}


def _run_command(
    argv: list[str], cwd: Path, timeout: int, env: dict[str, str]
) -> dict:
    """Run one command (already blocklist-approved) with timeout +
    output truncation.

    ``argv`` is either ["/bin/sh", "-c", command] or a bwrap-wrapped
    equivalent built by hydra.sandbox. No TTY, stdin closed, own
    process group so a timeout kills the whole pipeline (not just
    the shell).
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
        errors="replace",
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        stdout, stderr = proc.communicate()

    def _truncate(text: str) -> tuple[str, bool]:
        data = text.encode("utf-8", errors="replace")
        if len(data) > MAX_OUTPUT_BYTES:
            cut = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            return cut, True
        return text, False

    stdout, trunc_out = _truncate(stdout or "")
    stderr, trunc_err = _truncate(stderr or "")
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "truncated": trunc_out or trunc_err,
    }


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def make_server(config: Config, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHydraHandler", (HydraHandler,), {})
    handler.config = config
    handler.token = token
    handler.audit = AuditLog(config.audit_path)
    handler.store = MemoryStore(config.db_path)
    jobs = JobManager(data_dir=getattr(config, "data_dir", None))
    handler.jobs = jobs
    try:
        workflows = WorkflowManager(
            workspace=config.workspace,
            data_dir=getattr(config, "data_dir", None))
    except Exception as exc:  # degrade to in-memory, loudly (like jobs)
        print(f"[hydra] video workflow persistence unavailable "
              f"({exc}); running in-memory only", file=sys.stderr)
        workflows = WorkflowManager(workspace=config.workspace)
    handler.workflows = workflows
    handler.started_at = time.monotonic()
    # If the daemon process exits (Ctrl-C, shutdown), kill any running
    # NON-detached background jobs instead of orphaning them. Detached
    # jobs survive on purpose and are re-attached on the next start.
    atexit.register(jobs.kill_all)
    # Video workflows are interrupted (resumable), never orphaned:
    # a running melt is killed and the workflow can be resumed.
    atexit.register(workflows.interrupt_all)
    return ThreadingHTTPServer((config.bind, config.port), handler)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hydra",
        description="HYDRA v0.5 dumb-executor daemon (no model; outbound web only when asked).",
    )
    parser.add_argument("--bind", default=None,
                        help=f"bind address (default {DEFAULT_BIND}); "
                             "use 'tailscale' to resolve the current "
                             "Tailscale IPv4 via `tailscale ip -4`")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default {DEFAULT_PORT})")
    parser.add_argument("--workspace", default=None,
                        help="workspace dir (default ~/hydra-workspace)")
    parser.add_argument("--token-file", default=None,
                        help="token file (default ~/.local/share/hydra/token)")
    parser.add_argument("--data-dir", default=None,
                        help="data dir (default ~/.local/share/hydra)")
    parser.add_argument("--config", default=None,
                        help="optional JSON config file")
    parser.add_argument("--allow-public", action="store_true",
                        help="DANGEROUS: allow binding 0.0.0.0")
    parser.add_argument("--allow-systemctl-manage", action="store_true",
                        help="allow systemctl enable/start/stop via /exec")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        # Config construction resolves --bind tailscale (fail-closed
        # when Tailscale is down) and reads the optional config file.
        config = Config(args)
        validate_bind(config.bind, config.allow_public)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[hydra] refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        token = load_token(config.token_file)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        print(f"[hydra] refusing to start: {exc}", file=sys.stderr)
        print("[hydra] generate one with: python3 scripts/generate_token.py",
              file=sys.stderr)
        return 2

    server = make_server(config, token)
    pid_path = config.data_dir / "hydra.pid"
    try:
        pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
    except OSError as exc:
        print(f"[hydra] warning: could not write PID file: {exc}",
              file=sys.stderr)
        pid_path = None
    print("=" * 64)
    print("HYDRA daemon v0.4  —  DUMB EXECUTOR ONLY")
    print("No model. No Ollama. No LLM calls. Mikey (cloud) is the brain.")
    print("=" * 64)
    print(f"Listening on  http://{config.bind}:{config.port}")
    if getattr(config, "bind_mode", "explicit") == "tailscale":
        print(f"Bind mode     tailscale (resolved to {config.bind} "
              "via `tailscale ip -4`)")
    print(f"Workspace     {config.workspace}")
    print(f"Data dir      {config.data_dir}")
    print(f"Memory DB     {config.db_path}")
    print(f"Jobs DB       {config.data_dir / 'jobs.db'}")
    print(f"Video DB      {config.data_dir / 'video.db'}")
    print(f"Audit log     {config.audit_path}")
    print(f"PID file      {pid_path}")
    print(f"Token file    {config.token_file}  (never share this file)")
    if config.bind == "0.0.0.0":
        print("!!! WARNING: bound to 0.0.0.0 (--allow-public). Anyone who")
        print("!!! can reach this port AND knows the token can run commands")
        print("!!! inside the workspace. Prefer 127.0.0.1 / Tailscale IP.")
    print("Stop with: Ctrl-C, or POST /shutdown, or:")
    print("  systemctl --user stop hydra   (only if you installed the unit)")
    print("Detached jobs keep running after shutdown; see /jobs/list.")
    print("=" * 64, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hydra] stopping (Ctrl-C)", file=sys.stderr)
    finally:
        if pid_path is not None:
            try:
                if pid_path.read_text(
                        encoding="ascii").strip() == str(os.getpid()):
                    pid_path.unlink()
            except OSError:
                pass
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
