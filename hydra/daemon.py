"""HYDRA daemon v0.1: a DUMB executor for Baji's laptop.

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
import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .audit import AuditLog
from .memory_store import MemoryStore
from .security import (
    check_command_blocked,
    load_token,
    resolve_inside,
    resolve_readable,
    resolve_workspace,
    sha256_prefix,
    token_matches,
)

VERSION = "0.1.0"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
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
            self._send_json(200, {
                "ok": True, "service": "hydra-daemon", "version": VERSION,
            })
            return
        self._send_json(404, {"ok": False, "error": "unknown endpoint"})

    def do_POST(self):  # noqa: N802 - http.server naming
        path = urlparse(self.path).path
        routes = {
            "/exec": self._handle_exec,
            "/read": self._handle_read,
            "/write": self._handle_write,
            "/list": self._handle_list,
            "/memory/add": self._handle_memory_add,
            "/memory/search": self._handle_memory_search,
            "/memory/graph/add": self._handle_graph_add,
            "/memory/graph/neighbors": self._handle_graph_neighbors,
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
            self._send_json(400, {"ok": False, "error": str(exc)})
            self._audit(path, False, reason="bad_request")
            return
        handler(payload)

    # -- file + shell endpoints ------------------------------------------

    def _handle_exec(self, payload: dict) -> None:
        command = payload.get("command", "")
        if not isinstance(command, str):
            self._send_json(400, {"ok": False, "error": "command must be a string"})
            return
        cwd_param = payload.get("cwd", ".")
        try:
            cwd = resolve_inside(self.config.workspace, str(cwd_param or "."))
        except ValueError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)})
            self._audit("/exec", False, reason="cwd_escape")
            return
        try:
            timeout = int(payload.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        timeout = max(1, min(timeout, MAX_TIMEOUT_SECONDS))

        blocked = check_command_blocked(
            command, self.config.workspace,
            allow_systemctl_manage=self.config.allow_systemctl_manage,
            extra_patterns=self.config.extra_patterns,
        )
        audit_cmd = command[:200]
        if blocked:
            self._send_json(403, {
                "ok": False, "error": blocked, "blocked": True,
            })
            self._audit("/exec", False, reason="blocked",
                        command_preview=audit_cmd,
                        command_len=len(command))
            return

        # Scrubbed environment: HYDRA secrets never reach the child.
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("HYDRA_")
        }
        env.setdefault("TERM", "dumb")
        result = _run_command(command, cwd, timeout, env)
        self._send_json(200, {"ok": True, **result})
        self._audit("/exec", True, command_preview=audit_cmd,
                    command_len=len(command),
                    returncode=result["returncode"],
                    timed_out=result["timed_out"],
                    stdout_len=len(result["stdout"]),
                    stderr_len=len(result["stderr"]))

    def _handle_read(self, payload: dict) -> None:
        rel = str(payload.get("path", ""))
        try:
            max_bytes = int(payload.get("max_bytes", MAX_OUTPUT_BYTES))
        except (TypeError, ValueError):
            max_bytes = MAX_OUTPUT_BYTES
        max_bytes = max(1, min(max_bytes, MAX_REQUEST_BYTES))
        try:
            path, _ro = resolve_readable(
                self.config.workspace, rel, self.config.read_only_paths
            )
        except ValueError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)})
            self._audit("/read", False, reason="path_escape", path=rel[:256])
            return
        if not path.is_file():
            self._send_json(404, {"ok": False, "error": "not a file"})
            self._audit("/read", False, reason="not_found", path=rel[:256])
            return
        data = path.read_bytes()[: max_bytes + 1]
        truncated = len(data) > max_bytes
        data = data[:max_bytes]
        content = data.decode("utf-8", errors="replace")
        self._send_json(200, {
            "ok": True, "path": rel, "content": content,
            "truncated": truncated, "size": path.stat().st_size,
            "encoding": "utf-8-replace",
        })
        self._audit("/read", True, path=rel[:256], bytes_read=len(data),
                    sha256=sha256_prefix(data))

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
            self._send_json(403, {"ok": False, "error": str(exc)})
            self._audit("/write", False, reason="path_escape", path=rel[:256])
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
        self._send_json(200, {
            "ok": True, "path": rel, "bytes_written": len(data), "mode": mode,
        })
        self._audit("/write", True, path=rel[:256],
                    bytes_written=len(data), mode=mode,
                    sha256=sha256_prefix(data))

    def _handle_list(self, payload: dict) -> None:
        rel = str(payload.get("path", ".") or ".")
        recursive = bool(payload.get("recursive", False))
        try:
            path, _ro = resolve_readable(
                self.config.workspace, rel, self.config.read_only_paths
            )
        except ValueError as exc:
            self._send_json(403, {"ok": False, "error": str(exc)})
            self._audit("/list", False, reason="path_escape", path=rel[:256])
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
        self._send_json(200, {
            "ok": True, "path": rel, "entries": entries,
            "truncated": len(entries) >= MAX_LIST_ENTRIES,
        })
        self._audit("/list", True, path=rel[:256],
                    entries=len(entries), recursive=recursive)

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
        self._send_json(200, {"ok": True, "id": memory_id})
        self._audit("/memory/add", True, id=memory_id,
                    text_len=len(text),
                    sha256=sha256_prefix(text.encode("utf-8")))

    def _handle_memory_search(self, payload: dict) -> None:
        query = str(payload.get("query", ""))
        try:
            limit = int(payload.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        result = self.store.search(query, limit=limit)
        self._send_json(200, {"ok": True, **result})
        self._audit("/memory/search", True, query_len=len(query),
                    hits=len(result["results"]),
                    fts_available=result["fts_available"])

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
        self._send_json(200, {"ok": True, "id": rel_id})
        self._audit("/memory/graph/add", True,
                    subject=str(payload.get("subject", ""))[:128],
                    predicate=str(payload.get("predicate", ""))[:128],
                    object=str(payload.get("object", ""))[:128])

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
        self._send_json(200, {
            "ok": True, "entity": entity, "neighbors": neighbors,
        })
        self._audit("/memory/graph/neighbors", True,
                    entity=entity[:128], neighbors=len(neighbors))

    # -- shutdown ----------------------------------------------------------

    def _handle_shutdown(self, payload: dict) -> None:
        if payload.get("confirm") is not True:
            self._send_json(400, {
                "ok": False, "error": "shutdown needs {\"confirm\": true}",
            })
            return
        self._send_json(200, {"ok": True, "message": "shutting down"})
        self._audit("/shutdown", True)
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
    command: str, cwd: Path, timeout: int, env: dict[str, str]
) -> dict:
    """Run one shell command with timeout + output truncation.

    Uses /bin/sh -c AFTER the blocklist has approved the command.
    No TTY, stdin closed, own process group so a timeout kills the
    whole pipeline (not just the shell).
    """
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
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
    return ThreadingHTTPServer((config.bind, config.port), handler)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hydra",
        description="HYDRA v0.1 dumb-executor daemon (no model, no network calls).",
    )
    parser.add_argument("--bind", default=None,
                        help=f"bind address (default {DEFAULT_BIND})")
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
    config = Config(args)
    try:
        validate_bind(config.bind, config.allow_public)
    except ValueError as exc:
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
    print("=" * 64)
    print("HYDRA daemon v0.1  —  DUMB EXECUTOR ONLY")
    print("No model. No Ollama. No LLM calls. Mikey (cloud) is the brain.")
    print("=" * 64)
    print(f"Listening on  http://{config.bind}:{config.port}")
    print(f"Workspace     {config.workspace}")
    print(f"Data dir      {config.data_dir}")
    print(f"Memory DB     {config.db_path}")
    print(f"Audit log     {config.audit_path}")
    print(f"Token file    {config.token_file}  (never share this file)")
    if config.bind == "0.0.0.0":
        print("!!! WARNING: bound to 0.0.0.0 (--allow-public). Anyone who")
        print("!!! can reach this port AND knows the token can run commands")
        print("!!! inside the workspace. Prefer 127.0.0.1 / Tailscale IP.")
    print("Stop with: Ctrl-C, or POST /shutdown, or:")
    print("  systemctl --user stop hydra   (only if you installed the unit)")
    print("=" * 64, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hydra] stopping (Ctrl-C)", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
