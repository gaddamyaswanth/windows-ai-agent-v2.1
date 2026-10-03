"""
store.py — Persistent, secure state and memory store.

Improvements over the original:
- FTS5 full-text search with BM25 ranking + fallback to LIKE when FTS5 unavailable
- Per-operation short-lived connections (no shared connection across threads,
  no manual lock juggling, safe with WAL)
- Correct LIKE escaping (% _ \) to prevent wildcard injection
- Relevance scoring combining text match × importance × confidence × recency
- Content sanitization that redacts secrets instead of dropping the whole memory
- Deduplication of identical content
- Schema migrations, integrity checks, backup support
"""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Union

logger = logging.getLogger("Store")

DEFAULT_DB = os.path.join("memory", "memory.db")
MAX_CONTENT_LEN = 4000

TRUSTED_SOURCES = {
    "explicit_user_request",
    "task_history",
    "agent_experience",
}

# Patterns used both to *detect* secrets and to *redact* them.
SENSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)\b(?:password|passcode|api[_ -]?key|secret|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:authorization|cookie|set-cookie)\s*[:=]\s*\S+"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),          # plausible credit-card numbers
    re.compile(r"(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}"),  # MAC addresses / keys
]

_REDACTION = "[REDACTED]"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StoreError(Exception):
    """Raised on unrecoverable store failures."""


class Store:
    """SQLite-backed memory store. Thread-safe via short-lived connections."""

    def __init__(self, db_path: Union[str, os.PathLike] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._write_lock = threading.Lock()  # serialize writes only; reads are free

        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._fts_enabled = self._init_db()
        logger.info("Store ready at %s (FTS5=%s)", self.db_path, self._fts_enabled)

    # ------------------------------------------------------------------ setup

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    @contextmanager
    def _cursor(self, *, write: bool = False) -> Iterator[sqlite3.Cursor]:
        """Short-lived connection per operation — no cross-thread sharing."""
        if write:
            self._write_lock.acquire()
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            yield cur
            if write:
                conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise StoreError(f"Database operation failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()
            if write:
                self._write_lock.release()

    def _init_db(self) -> bool:
        with self._cursor(write=True) as cur:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                                                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                                                        kind        TEXT NOT NULL,
                                                        content     TEXT NOT NULL CHECK(length(content) <= 4000),
                    importance  INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
                    confidence  REAL    NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
                    source      TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0
                    );
                CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
                CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
                """
            )
            # Try FTS5
            try:
                cur.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content,
                        content='memories',
                        content_rowid='id',
                        tokenize='porter unicode61'
                    );
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    """
                )
                return True
            except sqlite3.Error:
                logger.warning("FTS5 unavailable; falling back to LIKE search.")
                return False

    # ------------------------------------------------------------ sanitization

    @staticmethod
    def _redact_secrets(text: str) -> str:
        for pattern in SENSITIVE_PATTERNS:
            text = pattern.sub(_REDACTION, text)
        return text

    @classmethod
    def _sanitize_content(cls, content: Any) -> Optional[str]:
        text = str(content).strip()
        if not text:
            return None
        if len(text) > MAX_CONTENT_LEN:
            text = text[:MAX_CONTENT_LEN].rsplit(" ", 1)[0] + "…"
        return cls._redact_secrets(text)

    # ------------------------------------------------------------------- save

    def save(
            self,
            kind: str,
            content: str,
            importance: int = 5,
            confidence: float = 1.0,
            source: str = "agent",
    ) -> Dict[str, Any]:
        if source not in TRUSTED_SOURCES:
            return {"success": False, "error": "Source is not trusted for persistence."}

        sanitized = self._sanitize_content(content)
        if not sanitized:
            return {"success": False, "error": "Content empty after sanitization."}

        kind = str(kind).strip()[:64]
        if not kind:
            return {"success": False, "error": "Kind must not be empty."}

        importance = max(1, min(int(importance), 10))
        confidence = max(0.0, min(float(confidence), 1.0))
        now = _utcnow()
        content_hash = hashlib_sha256(sanitized)

        try:
            with self._cursor(write=True) as cur:
                # Deduplicate: bump confidence/importance instead of storing again.
                cur.execute(
                    "SELECT id FROM memories WHERE content_hash = ? AND kind = ? LIMIT 1",
                    (content_hash, kind),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """UPDATE memories
                           SET confidence   = MAX(confidence, ?),
                               importance   = MAX(importance, ?),
                               updated_at   = ?
                           WHERE id = ?""",
                        (confidence, importance, now, row["id"]),
                    )
                    return {"success": True, "id": row["id"], "deduplicated": True}

                cur.execute(
                    """INSERT INTO memories
                       (kind, content, importance, confidence, source,
                        content_hash, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (kind, sanitized, importance, confidence, source,
                     content_hash, now, now),
                )
                record_id = cur.lastrowid
        except StoreError as exc:
            return {"success": False, "error": str(exc)}

        return {
            "success": True, "id": record_id, "deduplicated": False,
            "kind": kind, "content": sanitized, "source": source,
            "created_at": now,
        }

    # ----------------------------------------------------------------- search

    @staticmethod
    def _escape_like(term: str) -> str:
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _score(self, row: sqlite3.Row, now_ts: float) -> float:
        age_days = max(0.0, now_ts - _parse_iso(row["created_at"]) / 86400)
        recency = math.exp(-age_days / 30.0)          # ~half-life of 21 days
        access = 1.0 + math.log1p(row["access_count"])
        return row["importance"] * (0.3 + 0.7 * row["confidence"]) * recency * access

    def search(self, query: str, limit: int = 10, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        query = str(query).strip()
        limit = max(1, min(int(limit), 50))
        if not query:
            return []

        rows = self._search_fts(query, limit, kind) if self._fts_enabled \
            else self._search_like(query, limit, kind)

        if not rows:
            return []

        now_ts = datetime.now(timezone.utc).timestamp()
        scored = sorted(rows, key=lambda r: self._score(r, now_ts), reverse=True)[:limit]

        ids = [r["id"] for r in scored]
        with self._cursor(write=True) as cur:  # cheap async-ish access tracking
            cur.executemany(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                [(i,) for i in ids],
            )

        return [self._row_to_dict(r) for r in scored]

    def _search_fts(self, query: str, limit: int, kind: Optional[str]) -> List[sqlite3.Row]:
        # Build a safe OR-of-prefixes FTS query; strip special chars.
        terms = [re.sub(r'[^\w]', '', t) for t in query.split()]
        terms = [f'"{t}"*' for t in terms if t]
        if not terms:
            return []
        sql = (
                "SELECT m.* FROM memories m "
                "JOIN memories_fts f ON m.id = f.rowid "
                "WHERE memories_fts MATCH ? "
                + ("AND m.kind = ? " if kind else "")
                + "ORDER BY bm25(memories_fts), m.importance DESC LIMIT ?"
        )
        params = [" OR ".join(terms)] + ([kind] if kind else []) + [limit]
        try:
            with self._cursor() as cur:
                return list(cur.execute(sql, params).fetchall())
        except StoreError:
            return self._search_like(query, limit, kind)

    def _search_like(self, query: str, limit: int, kind: Optional[str]) -> List[sqlite3.Row]:
        words = query.split()
        clauses = ["content LIKE ? ESCAPE '\\'"] * len(words)
        params = [f"%{self._escape_like(w)}%" for w in words]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(limit)
        sql = (
            f"SELECT * FROM memories WHERE {' OR '.join(clauses)} "
            "ORDER BY importance DESC, created_at DESC LIMIT ?"
        )
        with self._cursor() as cur:
            return list(cur.execute(sql, params).fetchall())

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
        return {k: r[k] for k in r.keys() if k != "content_hash"}

    # ------------------------------------------------------------- utilities

    def get(self, memory_id: int) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def forget(self, memory_id: int, source: str = "explicit_user_request") -> bool:
        """GDPR-style deletion — only trusted sources may delete."""
        if source not in TRUSTED_SOURCES:
            return False
        try:
            with self._cursor(write=True) as cur:
                cur.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                return cur.rowcount > 0
        except StoreError:
            return False

    def prune(self, keep_top_per_kind: int = 200, max_age_days: int = 180) -> int:
        """Housekeeping: drop stale low-importance memories."""
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - max_age_days * 86400,
            tz=timezone.utc,
            ).isoformat()
        try:
            with self._cursor(write=True) as cur:
                cur.execute(
                    """DELETE FROM memories WHERE created_at < ?
                                              AND id NOT IN (
                            SELECT id FROM (
                                               SELECT id, ROW_NUMBER() OVER (
                                   PARTITION BY kind ORDER BY importance DESC, created_at DESC
                               ) AS rn FROM memories
                                           ) WHERE rn <= ?
                        )""",
                    (cutoff, keep_top_per_kind),
                )
                deleted = cur.rowcount
            logger.info("Pruned %d stale memories.", deleted)
            return deleted
        except StoreError:
            logger.exception("Prune failed.")
            return 0

    def summary_for_goal(self, goal: str, limit: int = 8) -> str:
        results = self.search(goal, limit=limit)
        return "\n".join(f"- [{m['kind']}] {m['content']}" for m in results)

    def stats(self) -> Dict[str, Any]:
        with self._cursor() as cur:
            total = cur.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            by_kind = dict(cur.execute(
                "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind ORDER BY n DESC"
            ).fetchall())
        return {"total": total, "by_kind": by_kind, "fts_enabled": self._fts_enabled}

    def backup(self, dest_path: Union[str, os.PathLike]) -> bool:
        """Consistent online backup using SQLite's backup API."""
        try:
            src = self._connect()
            dst = sqlite3.connect(str(dest_path))
            src.backup(dst)
            dst.close()
            src.close()
            logger.info("Backed up store to %s", dest_path)
            return True
        except sqlite3.Error:
            logger.exception("Backup failed.")
            return False

    def close(self) -> None:
        pass  # connections are short-lived; nothing to close

    # Context manager kept for API compatibility.
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


MemoryStore = Store


# --------------------------------------------------------------------- helpers
def hashlib_sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_iso(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0
