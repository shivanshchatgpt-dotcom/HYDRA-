"""Append-only audit log for HYDRA.

Every authenticated request is logged as one JSON line. The log
records WHAT happened, never secrets:
  * no Authorization header / token, ever
  * no full file contents (length + short hash only)
  * no full memory text (length + short hash only)
  * /exec commands truncated to 200 chars

The log lives under the data dir (~/.local/share/hydra/audit.jsonl)
and is append-only from the daemon's point of view: the daemon never
rewrites or deletes it. Baji can read/delete it himself.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, endpoint: str, client: str, ok: bool, **details) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": endpoint,
            "client": client,
            "ok": ok,
        }
        entry.update(details)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
