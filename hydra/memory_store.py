"""HYDRA small long-term memory store: keyword (FTS5) + graph retrieval.

NO embeddings. NO model. NO vector search. NO network calls.

This is deliberately NOT semantic RAG: Baji's hard rule is that no
local model and no LLM call may run on the laptop. All "thinking"
stays with Mikey in the cloud. This store only:
  * saves short facts Baji/Mikey explicitly store,
  * finds them again by KEYWORD (SQLite FTS5),
  * returns 1-hop graph neighbours for entities in those facts.

If FTS5 is unavailable in the local SQLite build, search falls back
to LIKE keyword matching and reports fts_available=false. That is a
retrieval fallback only; it never involves a model.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 AND query.

    Strips FTS5 syntax characters so user input can never inject
    operators. Returns "" when no searchable terms remain.
    """
    terms = re.findall(r"[A-Za-z0-9_]+", query or "")
    terms = [t for t in terms if len(t) >= 2][:10]
    if not terms:
        return ""
    # Quote each term and AND them: "laptop" AND "arch"
    return " AND ".join(f'"{t}"' for t in terms)


class MemoryStore:
    """Thread-safe SQLite memory + entity graph."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.fts_available = self._init_schema()

    # -- schema ----------------------------------------------------------

    def _init_schema(self) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'note',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    type TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    source_memory_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(subject, predicate, object)
                )
                """
            )
            # FTS5 mirror of memories.text. Content-sync triggers keep
            # the index consistent without any model involvement.
            try:
                cur.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(text, content='memories', content_rowid='id')
                    """
                )
                cur.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                    BEGIN
                      INSERT INTO memories_fts(rowid, text)
                      VALUES (new.id, new.text);
                    END
                    """
                )
                cur.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                    BEGIN
                      INSERT INTO memories_fts(memories_fts, rowid, text)
                      VALUES ('delete', old.id, old.text);
                    END
                    """
                )
                cur.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories
                    BEGIN
                      INSERT INTO memories_fts(memories_fts, rowid, text)
                      VALUES ('delete', old.id, old.text);
                      INSERT INTO memories_fts(rowid, text)
                      VALUES (new.id, new.text);
                    END
                    """
                )
                self._conn.commit()
                return True
            except sqlite3.OperationalError:
                # FTS5 not compiled in: LIKE search is used instead.
                self._conn.commit()
                return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ----------------------------------------------------------

    def add_memory(
        self,
        text: str,
        kind: str = "note",
        source: str = "",
        entities: list[dict] | None = None,
        relations: list[dict] | None = None,
    ) -> int:
        if not text or not text.strip():
            raise ValueError("memory text must not be empty")
        if len(text) > 8000:
            raise ValueError("memory text too long (max 8000 chars)")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO memories (text, kind, source, created_at)"
                " VALUES (?, ?, ?, ?)",
                (text.strip(), kind[:64], source[:256], _now()),
            )
            memory_id = cur.lastrowid
            for ent in entities or []:
                name = str(ent.get("name", "")).strip()
                if name:
                    cur.execute(
                        "INSERT OR IGNORE INTO entities (name, type)"
                        " VALUES (?, ?)",
                        (name[:256], str(ent.get("type", ""))[:64]),
                    )
            for rel in relations or []:
                self._add_relation_locked(
                    cur,
                    str(rel.get("subject", "")).strip(),
                    str(rel.get("predicate", "")).strip(),
                    str(rel.get("object", "")).strip(),
                    source_memory_id=memory_id,
                )
            self._conn.commit()
            return int(memory_id)

    def _add_relation_locked(
        self, cur: sqlite3.Cursor, subject: str, predicate: str,
        obj: str, source_memory_id: int | None,
    ) -> int | None:
        if not subject or not predicate or not obj:
            raise ValueError("relation needs subject, predicate, object")
        # Ensure both endpoints exist as entities (type unknown here).
        cur.execute(
            "INSERT OR IGNORE INTO entities (name, type) VALUES (?, '')",
            (subject[:256],),
        )
        cur.execute(
            "INSERT OR IGNORE INTO entities (name, type) VALUES (?, '')",
            (obj[:256],),
        )
        cur.execute(
            "INSERT OR IGNORE INTO relations"
            " (subject, predicate, object, source_memory_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (subject[:256], predicate[:128], obj[:256], source_memory_id, _now()),
        )
        return cur.lastrowid

    def add_relation(
        self, subject: str, predicate: str, obj: str,
        source_memory_id: int | None = None,
    ) -> int | None:
        with self._lock:
            cur = self._conn.cursor()
            rel_id = self._add_relation_locked(
                cur, subject.strip(), predicate.strip(), obj.strip(),
                source_memory_id,
            )
            self._conn.commit()
            return rel_id

    # -- deletes ---------------------------------------------------------

    def delete_memory(self, memory_id: int) -> bool:
        """Delete one memory by id (FTS index follows via trigger).

        Relations sourced from that memory are removed too; entities
        stay (other memories may mention them). Returns True when a
        row was actually deleted.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
            deleted = cur.rowcount > 0
            if deleted:
                cur.execute(
                    "DELETE FROM relations WHERE source_memory_id = ?",
                    (int(memory_id),))
            self._conn.commit()
            return deleted

    def delete_relation(self, relation_id: int) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM relations WHERE id = ?",
                        (int(relation_id),))
            deleted = cur.rowcount > 0
            self._conn.commit()
            return deleted

    # -- reads -----------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> dict:
        """Keyword search + 1-hop graph neighbours for matched entities."""
        limit = max(1, min(int(limit or 10), 50))
        with self._lock:
            cur = self._conn.cursor()
            rows: list[sqlite3.Row] = []
            if self.fts_available:
                fts_query = _sanitize_fts_query(query)
                if fts_query:
                    rows = cur.execute(
                        "SELECT m.id, m.text, m.kind, m.source, m.created_at,"
                        " bm25(memories_fts) AS rank"
                        " FROM memories_fts JOIN memories m"
                        "   ON memories_fts.rowid = m.id"
                        " WHERE memories_fts MATCH ?"
                        " ORDER BY rank LIMIT ?",
                        (fts_query, limit),
                    ).fetchall()
            else:
                like = f"%{query.strip()}%"
                rows = cur.execute(
                    "SELECT id, text, kind, source, created_at"
                    " FROM memories WHERE text LIKE ?"
                    " ORDER BY id DESC LIMIT ?",
                    (like, limit),
                ).fetchall()

            results = [dict(r) for r in rows]
            # Find entities mentioned in the matched texts, then their
            # 1-hop neighbours. Pure string matching, no model.
            entity_rows = cur.execute(
                "SELECT name FROM entities"
            ).fetchall()
            mentioned: list[str] = []
            for row in results:
                lowered = row["text"].lower()
                for ent in entity_rows:
                    name = ent["name"]
                    if name.lower() in lowered and name not in mentioned:
                        mentioned.append(name)
            graph: dict[str, list[dict]] = {}
            for name in mentioned[:10]:
                graph[name] = self._neighbors_locked(cur, name, limit=20)
            return {
                "query": query,
                "fts_available": self.fts_available,
                "results": results,
                "graph": graph,
            }

    def _neighbors_locked(
        self, cur: sqlite3.Cursor, entity: str, limit: int = 20
    ) -> list[dict]:
        rows = cur.execute(
            "SELECT subject, predicate, object, source_memory_id"
            " FROM relations"
            " WHERE subject = ? COLLATE NOCASE OR object = ? COLLATE NOCASE"
            " LIMIT ?",
            (entity, entity, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, entity: str, limit: int = 20) -> list[dict]:
        if not entity or not entity.strip():
            raise ValueError("entity must not be empty")
        limit = max(1, min(int(limit or 20), 100))
        with self._lock:
            cur = self._conn.cursor()
            return self._neighbors_locked(cur, entity.strip(), limit=limit)

    def list_recent(self, limit: int = 20, kind: str | None = None) -> dict:
        """Newest memories first, optionally filtered by kind."""
        limit = max(1, min(int(limit or 20), 100))
        with self._lock:
            cur = self._conn.cursor()
            if kind:
                rows = cur.execute(
                    "SELECT id, text, kind, source, created_at"
                    " FROM memories WHERE kind = ?"
                    " ORDER BY id DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
                total = cur.execute(
                    "SELECT COUNT(*) FROM memories WHERE kind = ?",
                    (kind,),
                ).fetchone()[0]
            else:
                rows = cur.execute(
                    "SELECT id, text, kind, source, created_at"
                    " FROM memories ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                total = cur.execute(
                    "SELECT COUNT(*) FROM memories").fetchone()[0]
            return {
                "memories": [dict(r) for r in rows],
                "count": len(rows),
                "total": int(total),
                "kind": kind,
            }
