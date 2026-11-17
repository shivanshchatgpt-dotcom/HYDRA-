"""HYDRA MCP server: expose the daemon's audited endpoints as MCP tools.

This is a thin stdio adapter, not a second implementation. Every tool
call is an HTTP+JSON request to the HYDRA daemon with the same bearer
token, the same workspace jail, the same blocklist, and the same audit
log. No model, no extra decisions, no state of its own.

Why: MCP hosts (Muse, Claude Code, other agents) can then use HYDRA as
their body without re-implementing file/shell/browser/video access.

Run (stdio, as MCP hosts expect):
    python3 -m hydra.mcp_server

Checks:
    python3 -m hydra.mcp_server --selfcheck
    python3 -m hydra.mcp_server --list-tools

Environment:
    HYDRA_URL         base URL (default http://127.0.0.1:8765)
    HYDRA_TOKEN_FILE  token path (default ~/.local/share/hydra/token)

Stdlib only. Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.4.0"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TOKEN_FILE = "~/.local/share/hydra/token"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _obj(props: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_OBJ = {"type": "object"}


# name -> (endpoint, description, inputSchema, payload keys)
# The payload for a call is built by copying the listed argument names
# straight through; MCP argument names are the daemon's payload names.
TOOLS: dict[str, tuple[str, str, dict, tuple[str, ...]]] = {
    "hydra_health": (
        "/health", "Daemon liveness, version, job counts, tool presence.",
        _obj({}), ()),
    # -- workspace files + shell -------------------------------------
    "hydra_exec": (
        "/exec",
        "Run a shell command inside the workspace jail (sync). "
        "Optional bubblewrap sandbox. Blocklist applies.",
        _obj({"command": _STR, "cwd": _STR, "timeout_seconds": _INT,
              "sandbox": _BOOL, "sandbox_no_network": _BOOL},
             ["command"]),
        ("command", "cwd", "timeout_seconds", "sandbox",
         "sandbox_no_network")),
    "hydra_read_file": (
        "/read", "Read a file inside the workspace (whole or line range).",
        _obj({"path": _STR, "max_bytes": _INT, "start_line": _INT,
              "end_line": _INT}, ["path"]),
        ("path", "max_bytes", "start_line", "end_line")),
    "hydra_write_file": (
        "/write", "Write a file inside the workspace (overwrite or append).",
        _obj({"path": _STR, "content": _STR, "mode": _STR},
             ["path", "content"]),
        ("path", "content", "mode")),
    "hydra_edit_file": (
        "/edit",
        "Replace an exact string in a workspace file. Supports dry_run and "
        "replace_all.",
        _obj({"path": _STR, "old_string": _STR, "new_string": _STR,
              "dry_run": _BOOL, "replace_all": _BOOL},
             ["path", "old_string", "new_string"]),
        ("path", "old_string", "new_string", "dry_run", "replace_all")),
    "hydra_list_dir": (
        "/list", "List a workspace directory (optionally recursive).",
        _obj({"path": _STR, "recursive": _BOOL}, ["path"]),
        ("path", "recursive")),
    "hydra_search": (
        "/search",
        "Search workspace contents (ripgrep when available, else grep).",
        _obj({"pattern": _STR, "path": _STR, "max_results": _INT,
              "glob": _STR, "fixed_strings": _BOOL,
              "case_sensitive": _BOOL}, ["pattern"]),
        ("pattern", "path", "max_results", "glob", "fixed_strings",
         "case_sensitive")),
    # -- background jobs ---------------------------------------------
    "hydra_job_start": (
        "/jobs/start",
        "Start a shell command as a background job (pollable; optionally "
        "detached so it survives daemon restarts).",
        _obj({"command": _STR, "cwd": _STR, "timeout_seconds": _INT,
              "sandbox": _BOOL, "detached": _BOOL}, ["command"]),
        ("command", "cwd", "timeout_seconds", "sandbox", "detached")),
    "hydra_job_poll": (
        "/jobs/poll", "Poll a job's status and new output.",
        _obj({"job_id": _STR, "offset": _INT}, ["job_id"]),
        ("job_id", "offset")),
    "hydra_job_kill": (
        "/jobs/kill", "Stop a running job.",
        _obj({"job_id": _STR}, ["job_id"]), ("job_id",)),
    "hydra_job_list": (
        "/jobs/list", "List jobs known to the daemon.",
        _obj({}), ()),
    # -- memory ------------------------------------------------------
    "hydra_memory_add": (
        "/memory/add", "Store a long-term memory note (optionally tagged).",
        _obj({"text": _STR, "kind": _STR, "source": _STR,
              "entities": {"type": "array"},
              "relations": {"type": "array"}}, ["text"]),
        ("text", "kind", "source", "entities", "relations")),
    "hydra_memory_search": (
        "/memory/search", "Full-text search over stored memories.",
        _obj({"query": _STR, "limit": _INT}, ["query"]),
        ("query", "limit")),
    "hydra_memory_list": (
        "/memory/list", "List recent memories.",
        _obj({"limit": _INT, "kind": _STR}), ("limit", "kind")),
    "hydra_memory_delete": (
        "/memory/delete", "Delete a memory or graph relation by id.",
        _obj({"id": _INT, "relation_id": _INT}), ("id", "relation_id")),
    # -- system (read-only) ------------------------------------------
    "hydra_system_info": (
        "/system/info", "Read-only host info (OS, kernel, CPU, memory, disk).",
        _obj({}), ()),
    "hydra_git_status": (
        "/git/status", "Read-only git status for a workspace path.",
        _obj({"path": _STR}, ["path"]), ("path",)),
    "hydra_tailscale_status": (
        "/system/tailscale", "Read-only Tailscale summary (no keys/peers).",
        _obj({}), ()),
    "hydra_doctor": (
        "/system/doctor", "Read-only self-checks: Tailscale, tools, disk.",
        _obj({}), ()),
    # -- screen / input (opt-in tools required) ----------------------
    "hydra_screen_shot": (
        "/screen/shot", "Take a desktop screenshot (needs spectacle or grim).",
        _obj({"save_to": _STR}), ("save_to",)),
    "hydra_input_move": (
        "/input/move", "Move the pointer (needs ydotool).",
        _obj({"x": _INT, "y": _INT}, ["x", "y"]), ("x", "y")),
    "hydra_input_click": (
        "/input/click", "Click at a screen position (needs ydotool).",
        _obj({"x": _INT, "y": _INT, "button": _STR, "clicks": _INT},
             ["x", "y"]),
        ("x", "y", "button", "clicks")),
    "hydra_input_type": (
        "/input/type", "Type text via the desktop input layer (needs ydotool).",
        _obj({"text": _STR}, ["text"]), ("text",)),
    # -- browser (Chrome over CDP, loopback only) --------------------
    "hydra_browser_status": (
        "/browser/status", "Check whether the loopback CDP browser is usable.",
        _obj({}), ()),
    "hydra_browser_navigate": (
        "/browser/navigate", "Navigate the CDP browser to a URL.",
        _obj({"url": _STR}, ["url"]), ("url",)),
    "hydra_browser_evaluate": (
        "/browser/evaluate", "Evaluate JavaScript in the CDP browser.",
        _obj({"expression": _STR}, ["expression"]), ("expression",)),
    "hydra_browser_content": (
        "/browser/content", "Read the current page's DOM/text.",
        _obj({}), ()),
    "hydra_browser_shot": (
        "/browser/shot", "Screenshot the current browser page.",
        _obj({"save_to": _STR}), ("save_to",)),
    "hydra_browser_click": (
        "/browser/click", "Click at page coordinates in the CDP browser.",
        _obj({"x": _INT, "y": _INT}, ["x", "y"]), ("x", "y")),
    "hydra_browser_type": (
        "/browser/type", "Type text into the CDP browser.",
        _obj({"text": _STR}, ["text"]), ("text",)),
    # -- headless video pipeline -------------------------------------
    "hydra_video_probe": (
        "/video/probe", "ffprobe a media file in the workspace.",
        _obj({"path": _STR}, ["path"]), ("path",)),
    "hydra_video_create_project": (
        "/video/project/create",
        "Validate a timeline spec, probe assets, write video/<name>/ "
        "(timeline.json, manifest.json, project.mlt). The spec is "
        "passed through exactly as given.",
        _obj({"name": _STR, "profile": _STR, "tracks": {"type": "array"},
              "transitions": {"type": "array"}, "subtitle": _STR,
              "assets": {"type": "array"}, "overwrite": _BOOL},
             ["name", "tracks"]),
        ("spec",)),
    "hydra_video_render_start": (
        "/video/render/start",
        "Queue a headless melt render of a created project.",
        _obj({"project_id": _STR, "output": _STR,
              "timeout_seconds": _INT, "dry_run": _BOOL},
             ["project_id"]),
        ("project_id", "output", "timeout_seconds", "dry_run")),
    "hydra_video_render_poll": (
        "/video/render/poll",
        "Poll a render workflow (send resume=true to resume after a restart).",
        _obj({"workflow_id": _STR, "resume": _BOOL}, ["workflow_id"]),
        ("workflow_id", "resume")),
    "hydra_video_preview": (
        "/video/preview", "Extract preview frames / contact sheet.",
        _obj({"path": _STR, "output_dir": _STR,
              "timestamps": {"type": "array"}}, ["path"]),
        ("path", "output_dir", "timestamps")),
    "hydra_video_list": (
        "/video/list", "List video projects and render workflows.",
        _obj({}), ()),
}


class HydraClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str,
                 payload: dict | None) -> tuple[int, dict]:
        data = (json.dumps(payload).encode("utf-8")
                if method == "POST" else None)
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES)
                return resp.status, json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            ray = exc.read(MAX_RESPONSE_BYTES)
            try:
                body = json.loads(ray.decode("utf-8"))
            except ValueError:
                body = {"ok": False, "error": ray.decode("utf-8", "replace")}
            return exc.code, body
        except urllib.error.URLError as exc:
            return 0, {"ok": False,
                       "error": f"daemon unreachable at {self.base_url}: "
                                f"{exc.reason}"}

    def call(self, tool: str, args: dict) -> tuple[int, dict]:
        endpoint, _, _, keys = TOOLS[tool]
        if keys == ("spec",):
            payload = {k: v for k, v in args.items()}
        else:
            payload = {k: args[k] for k in keys if k in args}
        method = "GET" if tool == "hydra_health" else "POST"
        return self._request(method, endpoint, payload)


def _load_token() -> str:
    path = os.environ.get("HYDRA_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"hydra mcp: cannot read token ({path}): {exc}")


def _tool_list() -> list[dict]:
    return [{"name": name,
             "description": desc,
             "inputSchema": schema}
            for name, (_ep, desc, schema, _keys) in TOOLS.items()]


def _jsonrpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def handle_message(client: HydraClient, msg: dict) -> dict | None:
    method = msg.get("method")
    msg_id = msg.get("id")
    if method is None:
        return None  # notification: nothing to answer
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hydra", "version": VERSION},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": _tool_list()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return _jsonrpc_error(msg_id, -32602, f"unknown tool: {name}")
        if not isinstance(args, dict):
            return _jsonrpc_error(msg_id, -32602, "arguments must be an object")
        status, body = client.call(name, args)
        ok = bool(body.get("ok")) and status < 400
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(body, ensure_ascii=False)}],
            "isError": not ok,
        }}
    return _jsonrpc_error(msg_id, -32601, f"unknown method: {method}")


def serve() -> int:
    client = HydraClient(os.environ.get("HYDRA_URL", DEFAULT_URL),
                         _load_token())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(_jsonrpc_error(None, -32700, "parse error")),
                  flush=True)
            continue
        reply = handle_message(client, msg)
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)
    return 0


def selfcheck() -> int:
    problems: list[str] = []
    seen: set[str] = set()
    for name, (endpoint, desc, schema, keys) in TOOLS.items():
        if name in seen:
            problems.append(f"duplicate tool name: {name}")
        seen.add(name)
        if not name.startswith("hydra_"):
            problems.append(f"{name}: tool names must start with hydra_")
        if not endpoint.startswith("/"):
            problems.append(f"{name}: bad endpoint {endpoint!r}")
        if not desc.strip():
            problems.append(f"{name}: empty description")
        if schema.get("type") != "object" or "properties" not in schema:
            problems.append(f"{name}: schema must be an object with properties")
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in props:
                problems.append(f"{name}: required {req!r} not in properties")
        for key in keys:
            if key != "spec" and key not in props:
                problems.append(f"{name}: payload key {key!r} not in schema")
    for problem in problems:
        print(f"FAIL {problem}")
    status = "OK" if not problems else "FAILED"
    print(f"tools={len(TOOLS)} schema={status}")
    if not problems:
        try:
            client = HydraClient(os.environ.get("HYDRA_URL", DEFAULT_URL),
                                 _load_token())
            code, body = client._request("GET", "/health", None)
            if code == 200 and body.get("ok"):
                print(f"daemon=reachable version={body.get('version')}")
            else:
                print(f"daemon=unreachable ({body.get('error', code)})"
                      " — start it with: python3 -m hydra")
        except SystemExit as exc:
            print(f"daemon=untested ({exc})")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HYDRA MCP server (thin stdio adapter over the daemon)")
    parser.add_argument("--selfcheck", action="store_true",
                        help="validate tool schemas and probe the daemon")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the MCP tool list as JSON")
    args = parser.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if args.list_tools:
        print(json.dumps(_tool_list(), ensure_ascii=False, indent=1))
        return 0
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
