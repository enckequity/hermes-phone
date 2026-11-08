"""SQLite store shared by the plugin and (from P2) the media bridge process.

The store accepts an injected connection or a path, so tests use tmp files and the bridge
opens the same database by path. ``plugin_db`` is imported lazily with a fallback because
Hermes is not importable in unit tests.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

PLUGIN_NAME = "hermes-phone"
SCHEMA_VERSION = 7
DB_ENV = "HERMES_PHONE_DB"

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  callee_name TEXT NOT NULL,
  number TEXT NOT NULL,
  number_source TEXT,
  goal TEXT NOT NULL,
  brief_json TEXT NOT NULL,
  user_id TEXT,
  session_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  approved_at REAL,
  dialed_at REAL,
  ended_at REAL,
  provider_sid TEXT,
  outcome TEXT,
  reason TEXT,
  result_json TEXT,
  result_delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_number ON calls(number, dialed_at);
CREATE TABLE IF NOT EXISTS approvals (
  call_id TEXT PRIMARY KEY,
  user_id TEXT,
  session_id TEXT,
  approved_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);
CREATE TABLE IF NOT EXISTS consents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  field TEXT NOT NULL,
  user_id TEXT,
  granted_at REAL NOT NULL,
  UNIQUE(call_id, field)
);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL,
  question TEXT NOT NULL,
  field TEXT,
  suggested TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  answer TEXT,
  created_at REAL NOT NULL,
  answered_at REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id, id);
CREATE TABLE IF NOT EXISTS transcripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  ts REAL NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_call ON transcripts(call_id, id);
"""

_SCHEMA_V2 = """
ALTER TABLE calls ADD COLUMN duration_s INTEGER;
CREATE TABLE IF NOT EXISTS media_tokens (
  token_hash TEXT PRIMARY KEY,
  call_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_media_tokens_call ON media_tokens(call_id);
"""

_SCHEMA_V3 = """
ALTER TABLE questions ADD COLUMN kind TEXT NOT NULL DEFAULT 'question';
ALTER TABLE questions ADD COLUMN consent_field TEXT;
ALTER TABLE questions ADD COLUMN card_posted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE questions ADD COLUMN late INTEGER NOT NULL DEFAULT 0;
ALTER TABLE calls ADD COLUMN hangup_requested_at REAL;
CREATE INDEX IF NOT EXISTS idx_questions_call ON questions(call_id, created_at);
"""

_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS web_tokens (
  token_hash TEXT PRIMARY KEY,
  call_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_web_tokens_call ON web_tokens(call_id);
"""

_SCHEMA_V5 = """
ALTER TABLE calls ADD COLUMN result_posted_at REAL;
ALTER TABLE calls ADD COLUMN retry_of TEXT;
ALTER TABLE calls ADD COLUMN disclosure_spoken INTEGER;
CREATE INDEX IF NOT EXISTS idx_calls_retry ON calls(retry_of);
"""

_SCHEMA_V6 = """
ALTER TABLE calls ADD COLUMN origin_session_id TEXT;
ALTER TABLE questions ADD COLUMN card_suggestion TEXT;
CREATE INDEX IF NOT EXISTS idx_calls_origin ON calls(origin_session_id, state);
"""

_SCHEMA_V7 = """
ALTER TABLE calls ADD COLUMN bridge_cursor TEXT;
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3, 4: _SCHEMA_V4,
                               5: _SCHEMA_V5, 6: _SCHEMA_V6, 7: _SCHEMA_V7}

# Columns callers may update through update_call.
_CALL_COLUMNS = {
    "state", "approved_at", "dialed_at", "ended_at", "provider_sid", "outcome", "reason",
    "result_json", "result_delivered", "user_id", "session_id", "duration_s",
    "hangup_requested_at", "result_posted_at", "bridge_cursor",
}


LIVE_STATES = ("dialing", "ringing", "in_progress", "on_hold_for_user", "voicemail")


def default_db_path() -> Path:
    """Path used when Hermes' ``plugin_db`` is unavailable (tests, the bridge process)."""
    override = os.environ.get(DB_ENV)
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "plugin-data" / PLUGIN_NAME / "data.db"


def open_connection(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open the store connection. With no path, prefer Hermes' ``plugin_db``."""
    if path is None and not os.environ.get(DB_ENV):
        try:  # lazy: Hermes is not installed in unit tests
            from plugins.plugin_storage import plugin_db  # type: ignore[import-not-found]

            return plugin_db(PLUGIN_NAME)
        except Exception:  # noqa: BLE001 - any failure falls back to a plain file
            pass
    target = Path(path) if path is not None else default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """Thin data-access layer. All writes are serialised by an in-process lock."""

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        path: str | os.PathLike | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self.conn = conn if conn is not None else open_connection(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    # -- schema ---------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            current = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for version in sorted(_MIGRATIONS):
                if version > current:
                    self.conn.executescript(_MIGRATIONS[version])
                    self.conn.execute(f"PRAGMA user_version={int(version)}")
                    self.conn.commit()

    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- helpers --------------------------------------------------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # -- calls ----------------------------------------------------------
    def create_call(self, call_id: str, *, state: str, callee_name: str, number: str,
                    number_source: str, goal: str, brief: dict, user_id: str = "",
                    session_id: str = "", retry_of: str = "") -> None:
        now = self.clock()
        self._exec(
            "INSERT INTO calls(id,state,callee_name,number,number_source,goal,brief_json,"
            "user_id,session_id,created_at,updated_at,retry_of,origin_session_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, state, callee_name, number, number_source, goal,
             json.dumps(brief), user_id, session_id, now, now, retry_of or None,
             session_id or None),
        )

    def get_call(self, call_id: str) -> dict | None:
        row = self._one("SELECT * FROM calls WHERE id=?", (call_id,))
        if row is not None:
            row["brief"] = json.loads(row.pop("brief_json") or "{}")
            row["result"] = json.loads(row["result_json"]) if row.get("result_json") else None
        return row

    def update_call(self, call_id: str, **fields: Any) -> None:
        bad = set(fields) - _CALL_COLUMNS
        if bad:
            raise ValueError(f"cannot update columns: {sorted(bad)}")
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE calls SET {cols} WHERE id=?", (*fields.values(), call_id))

    def update_brief(self, call_id: str, brief: dict) -> None:
        self._exec("UPDATE calls SET brief_json=?, updated_at=? WHERE id=?",
                   (json.dumps(brief), self.clock(), call_id))

    def update_state_if(self, call_id: str, expected: str, new: str, **fields: Any) -> bool:
        """Compare-and-set the state; returns False when the row was not in ``expected``."""
        bad = set(fields) - _CALL_COLUMNS
        if bad:
            raise ValueError(f"cannot update columns: {sorted(bad)}")
        fields.update(state=new, updated_at=self.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        cur = self._exec(f"UPDATE calls SET {cols} WHERE id=? AND state=?",
                         (*fields.values(), call_id, expected))
        return cur.rowcount == 1

    def recent_calls(self, limit: int = 10) -> list[dict]:
        rows = self._all("SELECT id FROM calls ORDER BY created_at DESC, rowid DESC LIMIT ?",
                         (int(limit),))
        return [c for c in (self.get_call(r["id"]) for r in rows) if c]

    def calls_in_states(self, states: Iterable[str]) -> list[dict]:
        states = list(states)
        if not states:
            return []
        marks = ",".join("?" * len(states))
        return self._all(f"SELECT * FROM calls WHERE state IN ({marks})", states)

    def count_dialed_since(self, since: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM calls WHERE dialed_at IS NOT NULL "
                        "AND dialed_at>=?", (since,))
        return int(row["n"]) if row else 0

    def last_dialed_to(self, number: str) -> float | None:
        row = self._one("SELECT MAX(dialed_at) AS t FROM calls WHERE number=? "
                        "AND dialed_at IS NOT NULL", (number,))
        return row["t"] if row and row["t"] is not None else None

    def undelivered_results(self) -> list[dict]:
        """Ended calls whose result nobody has reported yet (not posted by a watcher, not
        already handed to the agent)."""
        return self._all("SELECT * FROM calls WHERE result_delivered=0 AND ended_at IS NOT NULL "
                         "AND result_posted_at IS NULL ORDER BY ended_at")

    def mark_result_posted(self, call_id: str) -> bool:
        """Record that the result message went out. True only for the one writer that won."""
        now = self.clock()
        cur = self._exec("UPDATE calls SET result_posted_at=?, result_delivered=1, updated_at=? "
                         "WHERE id=? AND result_posted_at IS NULL", (now, now, call_id))
        return cur.rowcount == 1

    def retries_of(self, call_id: str) -> list[dict]:
        return self._all("SELECT id FROM calls WHERE retry_of=? ORDER BY created_at", (call_id,))

    def set_disclosure(self, call_id: str, spoken: bool) -> bool:
        """Record once whether the first agent line carried the AI disclosure."""
        cur = self._exec("UPDATE calls SET disclosure_spoken=? WHERE id=? AND "
                         "disclosure_spoken IS NULL", (1 if spoken else 0, call_id))
        return cur.rowcount == 1

    def set_result_once(self, call_id: str, result: dict, outcome: str = "") -> bool:
        """Store the call result only if none exists yet. True for the one writer that won."""
        cur = self._exec(
            "UPDATE calls SET result_json=?, outcome=COALESCE(NULLIF(?, ''), outcome), "
            "updated_at=? WHERE id=? AND result_json IS NULL",
            (json.dumps(result), outcome, self.clock(), call_id))
        return cur.rowcount == 1

    # -- media tokens (bridge websocket auth) ---------------------------
    def create_media_token(self, call_id: str, ttl_s: float) -> str:
        """Mint a single-use token; only its hash is stored."""
        token = secrets.token_urlsafe(24)
        now = self.clock()
        self._exec("INSERT INTO media_tokens(token_hash,call_id,created_at,expires_at) "
                   "VALUES(?,?,?,?)", (_hash(token), call_id, now, now + ttl_s))
        return token

    def consume_media_token(self, call_id: str, token: str) -> bool:
        now = self.clock()
        cur = self._exec(
            "UPDATE media_tokens SET used_at=? WHERE token_hash=? AND call_id=? "
            "AND used_at IS NULL AND expires_at>?", (now, _hash(token), call_id, now))
        return cur.rowcount == 1

    def media_token_used(self, call_id: str) -> bool:
        row = self._one("SELECT COUNT(*) AS n FROM media_tokens WHERE call_id=? "
                        "AND used_at IS NOT NULL", (call_id,))
        return bool(row and row["n"])

    def last_media_token_at(self, call_id: str) -> float | None:
        row = self._one("SELECT MAX(created_at) AS t FROM media_tokens WHERE call_id=?",
                        (call_id,))
        return row["t"] if row and row["t"] is not None else None

    # -- web-answer tokens (web phone mode: the link posted to the chat) -
    def create_web_token(self, call_id: str, ttl_s: float) -> str:
        """Mint the single-use link token for web phone mode; only its hash is stored."""
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._exec("INSERT INTO web_tokens(token_hash,call_id,created_at,expires_at) "
                   "VALUES(?,?,?,?)", (_hash(token), call_id, now, now + ttl_s))
        return token

    def _web_token_ok(self, call_id: str, token: str, now: float) -> bool:
        if not isinstance(token, str) or not token or len(token) > 200:
            return False
        want = _hash(token)
        found = False
        for row in self._all("SELECT token_hash,expires_at,used_at FROM web_tokens "
                             "WHERE call_id=?", (call_id,)):
            same = hmac.compare_digest(row["token_hash"], want)  # constant time per row
            if same and row["used_at"] is None and row["expires_at"] > now:
                found = True
        return found

    def peek_web_token(self, call_id: str, token: str) -> bool:
        """Is the token unused and unexpired? Does not consume it."""
        return self._web_token_ok(call_id, token, self.clock())

    def consume_web_token(self, call_id: str, token: str) -> bool:
        now = self.clock()
        if not self._web_token_ok(call_id, token, now):
            return False
        cur = self._exec("UPDATE web_tokens SET used_at=? WHERE token_hash=? AND call_id=? "
                         "AND used_at IS NULL AND expires_at>?",
                         (now, _hash(token), call_id, now))
        return cur.rowcount == 1

    def web_token_created_at(self, call_id: str) -> float | None:
        row = self._one("SELECT MAX(created_at) AS t FROM web_tokens WHERE call_id=?",
                        (call_id,))
        return row["t"] if row and row["t"] is not None else None

    # -- approvals ------------------------------------------------------
    def record_approval(self, call_id: str, *, user_id: str, session_id: str, ttl_s: float) -> None:
        now = self.clock()
        self._exec(
            "INSERT OR REPLACE INTO approvals(call_id,user_id,session_id,approved_at,expires_at,"
            "used_at) VALUES(?,?,?,?,?,NULL)",
            (call_id, user_id, session_id, now, now + ttl_s),
        )

    def get_approval(self, call_id: str) -> dict | None:
        return self._one("SELECT * FROM approvals WHERE call_id=?", (call_id,))

    def consume_approval(self, call_id: str) -> bool:
        """Mark the approval used. True only for the first caller (one-use)."""
        now = self.clock()
        cur = self._exec(
            "UPDATE approvals SET used_at=? WHERE call_id=? AND used_at IS NULL AND expires_at>?",
            (now, call_id, now),
        )
        return cur.rowcount == 1

    # -- consents -------------------------------------------------------
    def grant_consent(self, call_id: str, field: str, user_id: str = "") -> None:
        self._exec("INSERT OR IGNORE INTO consents(call_id,field,user_id,granted_at) "
                   "VALUES(?,?,?,?)", (call_id, field, user_id, self.clock()))

    def consents(self, call_id: str) -> set[str]:
        return {r["field"] for r in self._all("SELECT field FROM consents WHERE call_id=?",
                                              (call_id,))}

    # -- questions ------------------------------------------------------
    # status: open -> answered | declined | timeout (bridge gave up) -> late (answer after timeout)
    def add_question(self, qid: str, call_id: str, question: str, field: str = "",
                     suggested: str = "", *, kind: str = "question",
                     consent_field: str = "") -> None:
        self._exec("INSERT INTO questions(id,call_id,question,field,suggested,created_at,kind,"
                   "consent_field) VALUES(?,?,?,?,?,?,?,?)",
                   (qid, call_id, question, field, suggested, self.clock(), kind, consent_field))

    def get_question(self, qid: str) -> dict | None:
        return self._one("SELECT * FROM questions WHERE id=?", (qid,))

    def open_questions(self) -> list[dict]:
        return self._all("SELECT * FROM questions WHERE status='open' ORDER BY created_at")

    def questions_for_call(self, call_id: str) -> list[dict]:
        return self._all("SELECT * FROM questions WHERE call_id=? ORDER BY created_at, rowid",
                         (call_id,))

    def resolve_question(self, qid: str, status: str, answer: str | None = None) -> str | None:
        """Close an open question as ``answered``/``declined``/``timeout``. An answer that
        arrives after the timeout is stored and marked ``late``. Returns the final status, or
        None when the question is unknown or already closed (compare-and-set, one winner)."""
        now = self.clock()
        cur = self._exec("UPDATE questions SET status=?, answer=?, answered_at=? "
                         "WHERE id=? AND status='open'", (status, answer, now, qid))
        if cur.rowcount == 1:
            return status
        if status == "answered":
            cur = self._exec("UPDATE questions SET status='late', answer=?, answered_at=?, "
                             "late=1 WHERE id=? AND status='timeout'", (answer, now, qid))
            if cur.rowcount == 1:
                return "late"
        return None

    def mark_card_posted(self, qid: str, suggestion: str = "") -> None:
        """Card is out; keep the suggestion the user saw (a plain "yes" sends exactly it)."""
        self._exec("UPDATE questions SET card_posted=1, card_suggestion=? WHERE id=?",
                   (suggestion or None, qid))

    def open_questions_for_session(self, session_id: str) -> list[dict]:
        """Open questions of live calls that were started by this session (never another's)."""
        if not session_id:
            return []
        marks = ",".join("?" for _ in LIVE_STATES)
        return self._all(
            "SELECT q.* FROM questions q JOIN calls c ON c.id=q.call_id WHERE q.status='open' "
            f"AND c.origin_session_id=? AND c.state IN ({marks}) ORDER BY q.created_at, q.rowid",
            (session_id, *LIVE_STATES))

    def awaiting_for_session(self, session_id: str) -> list[dict]:
        """Calls this session prepared that still wait for approval (oldest first)."""
        if not session_id:
            return []
        return self._all("SELECT id FROM calls WHERE origin_session_id=? AND "
                         "state='awaiting_approval' ORDER BY created_at, rowid", (session_id,))

    def request_hangup(self, call_id: str) -> None:
        self._exec("UPDATE calls SET hangup_requested_at=COALESCE(hangup_requested_at, ?), "
                   "updated_at=? WHERE id=?", (self.clock(), self.clock(), call_id))

    def hangup_requested(self, call_id: str) -> bool:
        row = self._one("SELECT hangup_requested_at AS t FROM calls WHERE id=?", (call_id,))
        return bool(row and row["t"] is not None)

    # -- events and transcripts ----------------------------------------
    def add_event(self, call_id: str, kind: str, data: dict | None = None) -> None:
        self._exec("INSERT INTO events(call_id,ts,kind,data_json) VALUES(?,?,?,?)",
                   (call_id, self.clock(), kind, json.dumps(data or {})))

    def events(self, call_id: str) -> list[dict]:
        return self._all("SELECT * FROM events WHERE call_id=? ORDER BY id", (call_id,))

    def add_transcript_line(self, call_id: str, role: str, text: str) -> None:
        self._exec("INSERT INTO transcripts(call_id,ts,role,text) VALUES(?,?,?,?)",
                   (call_id, self.clock(), role, text))

    def transcript(self, call_id: str) -> list[dict]:
        return self._all("SELECT ts,role,text FROM transcripts WHERE call_id=? ORDER BY id",
                         (call_id,))
