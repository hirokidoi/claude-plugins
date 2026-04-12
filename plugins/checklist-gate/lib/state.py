"""SQLite DAO for checklist-gate plugin.

Provides the State class that encapsulates all database operations.
DB location: $CLAUDE_PLUGIN_DATA/checklist-gate.sqlite (WAL mode)
"""
import datetime
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional

# --- Dataclasses ---


@dataclass
class Session:
    session_id: str
    started_at: str
    cwd: str


@dataclass
class SessionCheck:
    id: int
    session_id: str
    item: str
    reason: str
    checked_at: str


@dataclass
class Ack:
    id: int
    session_id: str
    item: str
    reason: str
    created_at: str
    consumed_at: Optional[str]
    prompt_id: Optional[int] = None


@dataclass
class UserPrompt:
    id: int
    session_id: str
    prompt: str
    created_at: str


@dataclass
class GateDeny:
    session_id: str
    gate_name: str
    created_at: str


@dataclass
class GateToggle:
    id: int
    session_id: str
    gate_name: str
    enabled: bool
    updated_at: str


# --- Schema DDL ---

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    cwd        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    item       TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    checked_at TEXT    NOT NULL,
    UNIQUE(session_id, item)
);

CREATE TABLE IF NOT EXISTS acks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    item        TEXT    NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    consumed_at TEXT,
    prompt_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_acks_session_item_consumed
    ON acks(session_id, item, consumed_at);

CREATE TABLE IF NOT EXISTS user_prompts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    prompt     TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_prompts_session
    ON user_prompts(session_id);

CREATE TABLE IF NOT EXISTS gate_denies (
    session_id TEXT    NOT NULL,
    gate_name  TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (session_id, gate_name)
);

CREATE TABLE IF NOT EXISTS gate_toggles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    gate_name  TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT    NOT NULL,
    UNIQUE(session_id, gate_name)
);
"""


# --- State class ---


class State:
    """Thin DAO layer over the checklist-gate SQLite database.

    Responsibilities:
    - SQL execution and result mapping to dataclasses
    - Schema initialization
    - Transaction boundary support

    Gate evaluation logic belongs in the caller (gate_check.py), NOT here.
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.environ['CLAUDE_PLUGIN_DATA'], 'checklist-gate.sqlite'
            )
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            db_dir = os.path.dirname(self._db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA foreign_keys=ON')
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self):
        """Explicit transaction boundary for atomic multi-step operations."""
        conn = self._get_conn()
        conn.execute('BEGIN')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def init_schema(self) -> None:
        """Create all tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript(_SCHEMA_SQL)
        # Migration: add prompt_id column for existing databases
        try:
            conn.execute('ALTER TABLE acks ADD COLUMN prompt_id INTEGER')
        except sqlite3.OperationalError:
            pass  # Column already exists

    # --- helpers ---

    @staticmethod
    def _now() -> str:
        """Return current UTC time in ISO format."""
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    # --- Session operations ---

    def start_session(self, session_id: str, cwd: str) -> None:
        """Insert a new session into the sessions table."""
        conn = self._get_conn()
        conn.execute(
            'INSERT INTO sessions (session_id, started_at, cwd) VALUES (?, ?, ?)',
            (session_id, self._now(), cwd),
        )
        conn.commit()

    # --- SessionCheck operations (persistent ack) ---

    def add_session_check(
        self, session_id: str, item: str, reason: str
    ) -> None:
        """Insert a session check, ignoring duplicates."""
        conn = self._get_conn()
        conn.execute(
            'INSERT OR IGNORE INTO session_checks (session_id, item, reason, checked_at)'
            ' VALUES (?, ?, ?, ?)',
            (session_id, item, reason, self._now()),
        )
        conn.commit()

    def has_session_check(self, session_id: str, item: str) -> bool:
        """Return True if the session check exists."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT 1 FROM session_checks WHERE session_id = ? AND item = ?',
            (session_id, item),
        ).fetchone()
        return row is not None

    # --- Ack operations (consumable ack) ---

    def add_ack(
        self, session_id: str, item: str, reason: str,
        prompt_id: Optional[int] = None,
    ) -> None:
        """Add a new unconsumed ack."""
        conn = self._get_conn()
        conn.execute(
            'INSERT INTO acks (session_id, item, reason, created_at, prompt_id)'
            ' VALUES (?, ?, ?, ?, ?)',
            (session_id, item, reason, self._now(), prompt_id),
        )
        conn.commit()

    def has_unconsumed_ack(self, session_id: str, item: str) -> bool:
        """Return True if at least one unconsumed ack exists for the item."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL',
            (session_id, item),
        ).fetchone()
        return row is not None

    def get_oldest_unconsumed_ack(
        self, session_id: str, item: str
    ) -> Optional[Ack]:
        """Return the oldest unconsumed ack without consuming it."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT id, session_id, item, reason, created_at, consumed_at, prompt_id '
            'FROM acks '
            'WHERE session_id = ? AND item = ? AND consumed_at IS NULL '
            'ORDER BY created_at ASC LIMIT 1',
            (session_id, item),
        ).fetchone()
        if row is None:
            return None
        return Ack(
            id=row['id'],
            session_id=row['session_id'],
            item=row['item'],
            reason=row['reason'],
            created_at=row['created_at'],
            consumed_at=row['consumed_at'],
            prompt_id=row['prompt_id'],
        )

    def consume_oldest_unconsumed_ack(
        self, session_id: str, item: str
    ) -> Optional[Ack]:
        """Consume the oldest unconsumed ack (FIFO) within a transaction.

        Returns:
            The consumed Ack, or None if no unconsumed ack exists.
        """
        with self.transaction() as conn:
            row = conn.execute(
                'SELECT id, session_id, item, reason, created_at, consumed_at, prompt_id '
                'FROM acks '
                'WHERE session_id = ? AND item = ? AND consumed_at IS NULL '
                'ORDER BY created_at ASC LIMIT 1',
                (session_id, item),
            ).fetchone()
            if row is None:
                return None
            now = self._now()
            conn.execute(
                'UPDATE acks SET consumed_at = ? WHERE id = ?',
                (now, row['id']),
            )
            return Ack(
                id=row['id'],
                session_id=row['session_id'],
                item=row['item'],
                reason=row['reason'],
                created_at=row['created_at'],
                consumed_at=now,
                prompt_id=row['prompt_id'],
            )

    def list_unconsumed_acks(self, session_id: str) -> List[Ack]:
        """List all unconsumed acks for the session."""
        conn = self._get_conn()
        rows = conn.execute(
            'SELECT id, session_id, item, reason, created_at, consumed_at, prompt_id '
            'FROM acks WHERE session_id = ? AND consumed_at IS NULL '
            'ORDER BY created_at',
            (session_id,),
        ).fetchall()
        return [
            Ack(
                id=row['id'],
                session_id=row['session_id'],
                item=row['item'],
                reason=row['reason'],
                created_at=row['created_at'],
                consumed_at=row['consumed_at'],
                prompt_id=row['prompt_id'],
            )
            for row in rows
        ]

    # --- UserPrompt operations ---

    def add_user_prompt(self, session_id: str, prompt: str) -> int:
        """Insert a user prompt and return its id (prompt_id)."""
        conn = self._get_conn()
        cursor = conn.execute(
            'INSERT INTO user_prompts (session_id, prompt, created_at) VALUES (?, ?, ?)',
            (session_id, prompt, self._now()),
        )
        conn.commit()
        return cursor.lastrowid

    def get_user_prompt(self, prompt_id: int, session_id: str) -> Optional[UserPrompt]:
        """Return a user prompt by id, scoped to session_id."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT id, session_id, prompt, created_at '
            'FROM user_prompts WHERE id = ? AND session_id = ?',
            (prompt_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return UserPrompt(
            id=row['id'],
            session_id=row['session_id'],
            prompt=row['prompt'],
            created_at=row['created_at'],
        )

    def is_prompt_within_distance(
        self, prompt_id: int, session_id: str, max_distance: int
    ) -> bool:
        """Check if prompt_id is within the last max_distance prompts for the session."""
        conn = self._get_conn()
        rows = conn.execute(
            'SELECT id FROM user_prompts WHERE session_id = ? ORDER BY id DESC LIMIT ?',
            (session_id, max_distance),
        ).fetchall()
        recent_ids = {row['id'] for row in rows}
        return prompt_id in recent_ids

    def get_oldest_valid_prompt_id(
        self, session_id: str, max_distance: int
    ) -> Optional[int]:
        """Return the oldest prompt_id within the last max_distance prompts.

        Returns None if the session has no user_prompts.
        """
        conn = self._get_conn()
        rows = conn.execute(
            'SELECT id FROM user_prompts WHERE session_id = ? ORDER BY id DESC LIMIT ?',
            (session_id, max_distance),
        ).fetchall()
        if not rows:
            return None
        return min(row['id'] for row in rows)

    # --- GateDeny operations ---

    def record_deny(self, session_id: str, gate_name: str) -> None:
        """Record a deny event (UPSERT: keeps only the latest per session+gate)."""
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO gate_denies (session_id, gate_name, created_at) '
            'VALUES (?, ?, ?)',
            (session_id, gate_name, self._now()),
        )
        conn.commit()

    def has_deny(self, session_id: str, gate_name: str) -> bool:
        """Return True if a deny record exists for the session+gate."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT 1 FROM gate_denies WHERE session_id = ? AND gate_name = ?',
            (session_id, gate_name),
        ).fetchone()
        return row is not None

    def clear_deny(self, session_id: str, gate_name: str) -> None:
        """Delete the deny record for the session+gate (called after ack succeeds)."""
        conn = self._get_conn()
        conn.execute(
            'DELETE FROM gate_denies WHERE session_id = ? AND gate_name = ?',
            (session_id, gate_name),
        )
        conn.commit()

    def find_gates_requiring_item(self, item: str, gates: list) -> list:
        """Return gate names from the gates list that require the given ack item."""
        return [
            g.get('name', '')
            for g in gates
            if item in g.get('require', [])
        ]

    # --- GateToggle operations ---

    def set_gate_toggle(
        self, session_id: str, gate_name: str, enabled: bool
    ) -> None:
        """Upsert a gate toggle."""
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO gate_toggles (session_id, gate_name, enabled, updated_at)'
            ' VALUES (?, ?, ?, ?)',
            (session_id, gate_name, int(enabled), self._now()),
        )
        conn.commit()

    def is_gate_enabled(self, session_id: str, gate_name: str) -> bool:
        """Return whether the gate is enabled. Defaults to True if not set."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT enabled FROM gate_toggles WHERE session_id = ? AND gate_name = ?',
            (session_id, gate_name),
        ).fetchone()
        if row is None:
            return True
        return bool(row['enabled'])

    def list_gate_toggles(self, session_id: str) -> List[GateToggle]:
        """List all gate toggles for the session."""
        conn = self._get_conn()
        rows = conn.execute(
            'SELECT id, session_id, gate_name, enabled, updated_at '
            'FROM gate_toggles WHERE session_id = ?',
            (session_id,),
        ).fetchall()
        return [
            GateToggle(
                id=row['id'],
                session_id=row['session_id'],
                gate_name=row['gate_name'],
                enabled=bool(row['enabled']),
                updated_at=row['updated_at'],
            )
            for row in rows
        ]

    def cleanup_old_gate_toggles(self, keep_days: int = 7) -> int:
        """Delete gate_toggles belonging to sessions started more than keep_days ago.

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM gate_toggles WHERE session_id IN '
            '(SELECT session_id FROM sessions WHERE started_at < ?)',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_old_session_checks(self, keep_days: int = 30) -> int:
        """Delete session_checks belonging to sessions started more than keep_days ago.

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM session_checks WHERE session_id IN '
            '(SELECT session_id FROM sessions WHERE started_at < ?)',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_old_acks(self, keep_days: int = 30) -> int:
        """Delete acks belonging to sessions started more than keep_days ago.

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM acks WHERE session_id IN '
            '(SELECT session_id FROM sessions WHERE started_at < ?)',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_old_user_prompts(self, keep_days: int = 7) -> int:
        """Delete user_prompts belonging to sessions started more than keep_days ago.

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM user_prompts WHERE session_id IN '
            '(SELECT session_id FROM sessions WHERE started_at < ?)',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_old_gate_denies(self, keep_days: int = 7) -> int:
        """Delete gate_denies belonging to sessions started more than keep_days ago.

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM gate_denies WHERE session_id IN '
            '(SELECT session_id FROM sessions WHERE started_at < ?)',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount

    def cleanup_old_sessions(self, keep_days: int = 30) -> int:
        """Delete sessions started more than keep_days ago.

        Call after cleaning up child tables (session_checks, acks, gate_toggles).

        Returns:
            Number of deleted rows.
        """
        conn = self._get_conn()
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=keep_days)
        ).isoformat()
        cursor = conn.execute(
            'DELETE FROM sessions WHERE started_at < ?',
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount
