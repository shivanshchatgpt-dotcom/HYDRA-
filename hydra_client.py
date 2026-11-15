#!/usr/bin/env python3
"""Small stdlib client for the HYDRA daemon (for Baji to test with).

Reads the token from HYDRA_TOKEN_FILE (default
~/.local/share/hydra/token). Never prints the token.

Examples:
    python3 hydra_client.py health
    python3 hydra_client.py exec --command "ls -la"
    python3 hydra_client.py read --path notes.txt
    python3 hydra_client.py write --path hello.txt --content "hi"
    python3 hydra_client.py list --path .
    python3 hydra_client.py memory-add --text "Baji uses Arch Linux" --kind fact
    python3 hydra_client.py memory-search --query "Arch Linux"
    python3 hydra_client.py graph-neighbors --entity "Baji"

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
    p = sub.add_parser("read")
    p.add_argument("--path", required=True)
    p = sub.add_parser("write")
    p.add_argument("--path", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--mode", default="overwrite", choices=["overwrite", "append"])
    p = sub.add_parser("list")
    p.add_argument("--path", default=".")
    p.add_argument("--recursive", action="store_true")
    p = sub.add_parser("memory-add")
    p.add_argument("--text", required=True)
    p.add_argument("--kind", default="note")
    p.add_argument("--source", default="client")
    p = sub.add_parser("memory-search")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("graph-add")
    p.add_argument("--subject", required=True)
    p.add_argument("--predicate", required=True)
    p.add_argument("--object", required=True)
    p = sub.add_parser("graph-neighbors")
    p.add_argument("--entity", required=True)
    sub.add_parser("shutdown")

    args = parser.parse_args(argv)
    token = load_token(args.token_file)

    if args.cmd == "health":
        # /health needs no auth, but sending the token is harmless.
        result = call(args.url, token, "/health", None)
    elif args.cmd == "exec":
        result = call(args.url, token, "/exec", {
            "command": args.command, "cwd": args.cwd,
            "timeout_seconds": args.timeout})
    elif args.cmd == "read":
        result = call(args.url, token, "/read", {"path": args.path})
    elif args.cmd == "write":
        result = call(args.url, token, "/write", {
            "path": args.path, "content": args.content, "mode": args.mode})
    elif args.cmd == "list":
        result = call(args.url, token, "/list", {
            "path": args.path, "recursive": args.recursive})
    elif args.cmd == "memory-add":
        result = call(args.url, token, "/memory/add", {
            "text": args.text, "kind": args.kind, "source": args.source})
    elif args.cmd == "memory-search":
        result = call(args.url, token, "/memory/search", {
            "query": args.query, "limit": args.limit})
    elif args.cmd == "graph-add":
        result = call(args.url, token, "/memory/graph/add", {
            "subject": args.subject, "predicate": args.predicate,
            "object": args.object})
    elif args.cmd == "graph-neighbors":
        result = call(args.url, token, "/memory/graph/neighbors", {
            "entity": args.entity})
    elif args.cmd == "shutdown":
        result = call(args.url, token, "/shutdown", {"confirm": True})
    else:  # pragma: no cover
        parser.error("unknown command")

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
