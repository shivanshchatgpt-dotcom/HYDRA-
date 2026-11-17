# MCP_SETUP.md — using HYDRA from an MCP host (v0.5)

`hydra/mcp_server.py` is a thin **stdio MCP adapter** over the HYDRA
daemon. It does not duplicate any logic: every tool call becomes one
HTTP+JSON request to the running daemon, with the same bearer token,
the same workspace jail, the same command blocklist, and the same
audit log. No model, no decisions, no state of its own.

That means any MCP-capable agent (Muse, Claude Code, Codex, a custom
host) can drive the laptop through HYDRA, and Baji still has one
obvious kill switch (`scripts/kill-switch.sh`, or stop the daemon).

## What it exposes

36 tools, all prefixed `hydra_`:

| Group | Tools |
|---|---|
| health | `hydra_health` |
| files + shell | `hydra_exec`, `hydra_read_file`, `hydra_write_file`, `hydra_edit_file`, `hydra_list_dir`, `hydra_search` |
| jobs | `hydra_job_start`, `hydra_job_poll`, `hydra_job_kill`, `hydra_job_list` |
| memory | `hydra_memory_add`, `hydra_memory_search`, `hydra_memory_list`, `hydra_memory_delete` |
| system (read-only) | `hydra_system_info`, `hydra_git_status`, `hydra_tailscale_status`, `hydra_doctor` |
| screen + input (opt-in tools) | `hydra_screen_shot`, `hydra_input_move`, `hydra_input_click`, `hydra_input_type` |
| browser (CDP, loopback only) | `hydra_browser_status`, `hydra_browser_navigate`, `hydra_browser_evaluate`, `hydra_browser_content`, `hydra_browser_shot`, `hydra_browser_click`, `hydra_browser_type` |
| video (headless) | `hydra_video_probe`, `hydra_video_create_project`, `hydra_video_render_start`, `hydra_video_render_poll`, `hydra_video_preview`, `hydra_video_list` |

All limits come from the daemon and are unchanged: jail, blocklist,
sandbox option, token, audit-before-response.

## Requirements

- The daemon must be running (`./scripts/start-daemon.sh` or
  `python3 -m hydra`), reachable at `HYDRA_URL`
  (default `http://127.0.0.1:8765`).
- A token file (`HYDRA_TOKEN_FILE`, default
  `~/.local/share/hydra/token`).
- Python 3.11+ (stdlib only). Nothing to install.

## Self-checks

```bash
python3 -m hydra.mcp_server --selfcheck     # 36 tool schemas + daemon probe
python3 -m hydra.mcp_server --list-tools    # JSON list for inspection
```

## Host configuration

The server speaks newline-delimited JSON-RPC on stdin/stdout, which is
what MCP hosts expect for stdio servers.

### Muse Code (`~/.config/muse/settings.json`)

```json
{
  "mcpServers": {
    "hydra": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "hydra.mcp_server"],
      "env": {
        "HYDRA_URL": "http://127.0.0.1:8765",
        "HYDRA_TOKEN_FILE": "/home/baji/.local/share/hydra/token",
        "PYTHONPATH": "/home/baji/hydra-harness"
      }
    }
  }
}
```

(`PYTHONPATH` is only needed when the host's working directory is not
the repo root.)

### Claude Code / `.mcp.json`

```json
{
  "mcpServers": {
    "hydra": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "hydra.mcp_server"],
      "env": {"PYTHONPATH": "/home/baji/hydra-harness"}
    }
  }
}
```

## Security notes

- The adapter adds **no** privileges. Anything a tool can do, the
  daemon could already do for Mikey.
- Screen/input tools need `spectacle`/`grim` and `ydotool`; without
  them they fail loudly instead of pretending.
- Browser tools refuse non-loopback CDP targets (`docs/BROWSER_SETUP.md`).
- Tokens are read from a file, never logged, never passed in argv.
- Audit entries are written by the daemon before the response, so the
  log reflects dropped/failed calls too.

## Known limits

- One request at a time per tool call; long renders are polled via
  `hydra_video_render_poll` (`resume: true` after a restart).
- No streaming; large outputs are truncated/capped by the daemon.
- The daemon must already be up. The MCP adapter never starts it:
  starting services is a decision, not a tool call.
