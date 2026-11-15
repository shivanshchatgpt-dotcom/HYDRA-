#!/usr/bin/env python3
"""Generate a HYDRA bearer token file with 0600 permissions.

Usage:
    python3 scripts/generate_token.py [--token-file PATH] [--force]

Refuses to overwrite an existing token unless --force is given.
Never prints the token. The daemon refuses to start if the token
file is readable by group/other.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a HYDRA token file")
    parser.add_argument(
        "--token-file",
        default=os.environ.get(
            "HYDRA_TOKEN_FILE", "~/.local/share/hydra/token"),
    )
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing token file")
    args = parser.parse_args()

    path = Path(args.token_file).expanduser()
    if path.exists() and not args.force:
        print(f"token file already exists: {path} (use --force to rotate)",
              file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Write with O_CREAT|O_EXCL semantics when not forcing, then chmod.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"unexpected token file mode {oct(mode)}"
    print(f"wrote token file: {path} (mode 0600)")
    print("Do NOT share, commit, or paste this file anywhere.")
    print("To rotate: re-run with --force, then restart the daemon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
