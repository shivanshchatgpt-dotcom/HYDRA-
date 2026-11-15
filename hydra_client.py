#!/usr/bin/env python3
"""Small stdlib client for the HYDRA daemon (for Baji to test with).

Reads the token from HYDRA_TOKEN_FILE (default
~/.local/share/hydra/token). Never prints the token.

Examples:
    python3 hydra_client.py health
    python3 hydra_client.py exec --command "ls -la"
    python3 hydra_client.py exec --command "make" --sandbox
    python3 hydra_client.py read --path notes.txt --start-line 10 --end-line 20
    python3 hydra_client.py write --path hello.txt --content "hi"
    python3 hydra_client.py edit --path hello.txt --old "hi" --new "hello"
    python3 hydra_client.py search --pattern "TODO" --glob "*.py"
    python3 hydra_client.py list --path .
    python3 hydra_client.py jobs-start --command "sleep 60"
    python3 hydra_client.py jobs-poll --job-id <id>
    python3 hydra_client.py jobs-kill --job-id <id>
    python3 hydra_client.py jobs-list
    python3 hydra_client.py memory-add --text "Baji uses Arch Linux" --kind fact
    python3 hydra_client.py memory-search --query "Arch Linux"
    python3 hydra_client.py memory-list
    python3 hydra_client.py memory-delete --id 3
    python3 hydra_client.py graph-neighbors --entity "Baji"
    python3 hydra_client.py screen-shot --save-to shots/shot.png
    python3 hydra_client.py input-move --x 100 --y 200
    python3 hydra_client.py input-click --x 100 --y 200
    python3 hydra_client.py input-type --text "hello"

Over Tailscale (from another machine on the same tailnet):
    python3 hydra_client.py --url http://100.x.y.z:8765 health
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def load_token(token_file: str) -> str:
    return Path(token_file).expanduser().read_text(encoding="utf-8").strip()


def call(base_url: str, token: str, path: str, payload: dict | None) -> dict:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {body}", file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HYDRA daemon client")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", default=os.environ.get(
        "HYDRA_TOKEN_FILE", "~/.local/share/hydra/token"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    p = sub.add_parser("exec")
    p.add_argument("--command", required=True)
    p.add_argument("--cwd", default=".")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--sandbox", action="store_true",
                   help="run inside a bubblewrap sandbox (if installed)")
    p.add_argument("--sandbox-allow-network", action="store_true",
                   help="with --sandbox: keep network access inside it")
    p = sub.add_parser("read")
    p.add_argument("--path", required=True)
    p.add_argument("--start-line", type=int, default=None)
    p.add_argument("--end-line", type=int, default=None)
    p = sub.add_parser("write")
    p.add_argument("--path", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--mode", default="overwrite", choices=["overwrite", "append"])
    p = sub.add_parser("edit")
    p.add_argument("--path", required=True)
    p.add_argument("--old", required=True, dest="old_string")
    p.add_argument("--new", required=True, dest="new_string")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--replace-all", action="store_true")
    p = sub.add_parser("search")
    p.add_argument("--pattern", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--glob", default=None)
    p.add_argument("--fixed-strings", action="store_true")
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--max-results", type=int, default=100)
    p = sub.add_parser("list")
    p.add_argument("--path", default=".")
    p.add_argument("--recursive", action="store_true")
    p = sub.add_parser("jobs-start")
    p.add_argument("--command", required=True)
    p.add_argument("--cwd", default=".")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--sandbox", action="store_true")
    p = sub.add_parser("jobs-poll")
    p.add_argument("--job-id", required=True)
    p.add_argument("--offset", type=int, default=0)
    p = sub.add_parser("jobs-kill")
    p.add_argument("--job-id", required=True)
    sub.add_parser("jobs-list")
    p = sub.add_parser("memory-add")
    p.add_argument("--text", required=True)
    p.add_argument("--kind", default="note")
    p.add_argument("--source", default="client")
    p = sub.add_parser("memory-search")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("memory-delete")
    p.add_argument("--id", type=int, default=None)
    p.add_argument("--relation-id", type=int, default=None)
    p = sub.add_parser("memory-list")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--kind", default=None)
    p = sub.add_parser("graph-add")
    p.add_argument("--subject", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--object", required=True)
    p = sub.add_parser("graph-neighbors")
    p.add_argument("--entity", required=True)
    p = sub.add_parser("screen-shot")
    p.add_argument("--save-to", default=None)
    p = sub.add_parser("input-move")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p = sub.add_parser("input-click")
    p.add_argument("--x", type=int, default=None)
    p.add_argument("--y", type=int, default=None)
    p.add_argument("--button", default="left",
                   choices=["left", "middle", "right"])
    p.add_argument("--clicks", type=int, default=1)
    p = sub.add_parser("input-type")
    p.add_argument("--text", required=True)
    sub.add_parser("shutdown")

    args = parser.parse_args(argv)
    token = load_token(args.token_file)

    if args.cmd == "health":
        # /health needs no auth, but sending the token is harmless.
        result = call(args.url, token, "/health", None)
    elif args.cmd == "exec":
        result = call(args.url, token, "/exec", {
            "command": args.command, "cwd": args.cwd,
            "timeout_seconds": args.timeout,
            "sandbox": args.sandbox,
            "sandbox_no_network": not args.sandbox_allow_network})
    elif args.cmd == "read":
        payload: dict = {"path": args.path}
        if args.start_line is not None:
            payload["start_line"] = args.start_line
        if args.end_line is not None:
            payload["end_line"] = args.end_line
        result = call(args.url, token, "/read", payload)
    elif args.cmd == "write":
        result = call(args.url, token, "/write", {
            "path": args.path, "content": args.content, "mode": args.mode})
    elif args.cmd == "edit":
        result = call(args.url, token, "/edit", {
            "path": args.path, "old_string": args.old_string,
            "new_string": args.new_string, "dry_run": args.dry_run,
            "replace_all": args.replace_all})
    elif args.cmd == "search":
        result = call(args.url, token, "/search", {
            "pattern": args.pattern, "path": args.path, "glob": args.glob,
            "fixed_strings": args.fixed_strings,
            "case_sensitive": not args.ignore_case,
            "max_results": args.max_results})
    elif args.cmd == "list":
        result = call(args.url, token, "/list", {
            "path": args.path, "recursive": args.recursive})
    elif args.cmd == "jobs-start":
        result = call(args.url, token, "/jobs/start", {
            "command": args.command, "cwd": args.cwd,
            "timeout_seconds": args.timeout, "sandbox": args.sandbox})
    elif args.cmd == "jobs-poll":
        result = call(args.url, token, "/jobs/poll", {
            "job_id": args.job_id, "offset": args.offset})
    elif args.cmd == "jobs-kill":
        result = call(args.url, token, "/jobs/kill", {"job_id": args.job_id})
    elif args.cmd == "jobs-list":
        result = call(args.url, token, "/jobs/list", {})
    elif args.cmd == "memory-add":
        result = call(args.url, token, "/memory/add", {
            "text": args.text, "kind": args.kind, "source": args.source})
    elif args.cmd == "memory-search":
        result = call(args.url, token, "/memory/search", {
            "query": args.query, "limit": args.limit})
    elif args.cmd == "memory-delete":
        payload = {}
        if args.id is not None:
            payload["id"] = args.id
        if args.relation_id is not None:
            payload["relation_id"] = args.relation_id
        result = call(args.url, token, "/memory/delete", payload)
    elif args.cmd == "memory-list":
        result = call(args.url, token, "/memory/list", {
            "limit": args.limit, "kind": args.kind})
    elif args.cmd == "graph-add":
        result = call(args.url, token, "/memory/graph/add", {
            "subject": args.subject, "predicate": args.predicate,
            "object": args.object})
    elif args.cmd == "graph-neighbors":
        result = call(args.url, token, "/memory/graph/neighbors", {
            "entity": args.entity})
    elif args.cmd == "screen-shot":
        payload = {}
        if args.save_to:
            payload["save_to"] = args.save_to
        result = call(args.url, token, "/screen/shot", payload)
        if "data_base64" in result:
            # Don't dump megabytes of base64 to the terminal.
            result = {k: ("<base64 png, %d bytes>" % len(v) if k == "data_base64" else v)
                      for k, v in result.items()}
    elif args.cmd == "input-move":
        result = call(args.url, token, "/input/move", {
            "x": args.x, "y": args.y})
    elif args.cmd == "input-click":
        result = call(args.url, token, "/input/click", {
            "x": args.x, "y": args.y, "button": args.button,
            "clicks": args.clicks})
    elif args.cmd == "input-type":
        result = call(args.url, token, "/input/type", {"text": args.text})
    elif args.cmd == "shutdown":
        result = call(args.url, token, "/shutdown", {"confirm": True})
    else:  # pragma: no cover
        parser.error("unknown command")

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
