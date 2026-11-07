"""SQLite DAO for checklist-gate plugin.

Provides the State class that encapsulates all database operations.
DB location: $CLAUDE_PLUGIN_DATA/checklist-gate.sqlite (WAL mode)
"""
import datetime
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional

BUSY_TIMEOUT_SECONDS = 5.0

# Retention in days per table; sessions are kept for the longest of them.
_RETENTION_DAYS = {
    'user_prompts': 7,
    'gate_toggles': 7,
    'gate_denies': 7,
    'nudge_firings': 7,
    'gate_passes': 7,
    'pending_effects': 7,
    'session_checks': 30,
    'acks': 30,
}
# Time column used to age out rows whose session row is gone.
_ORPHAN_TIME_COLUMN = {
    'user_prompts': 'created_at',
    'gate_toggles': 'updated_at',
    'gate_denies': 'created_at',
    'session_checks': 'checked_at',
    'acks': 'created_at',
    'pending_effects': 'created_at',
}
_MIGRATIONS = (
    ('acks', 'prompt_id', 'INTEGER'),
    ('sessions', 'last_active_at', 'TEXT'),
)

# --- Dataclasses ---


@dataclass
class UserPrompt:
    id: int
    session_id: str
    prompt: str
    created_at: str


@dataclass
class GateToggle:
    id: int
    session_id: str
    gate_name: str
    enabled: bool
    updated_at: str


@dataclass
class PendingEffect:
    kind: str
    name: str
    payload: dict


# --- Schema DDL ---

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    last_active_at TEXT
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

CREATE TABLE IF NOT EXISTS nudge_firings (
    session_id TEXT    NOT NULL,
    nudge_name TEXT    NOT NULL,
    prompt_id  INTEGER NOT NULL,
    PRIMARY KEY (session_id, nudge_name)
);

CREATE TABLE IF NOT EXISTS gate_passes (
    session_id TEXT    NOT NULL,
    gate_name  TEXT    NOT NULL,
    prompt_id  INTEGER NOT NULL,
    PRIMARY KEY (session_id, gate_name)
);

CREATE TABLE IF NOT EXISTS pending_effects (
    session_id  TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, tool_use_id, kind, name)
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

    def __init__(self, db_path: Optional[str] = None) -> None:
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
            self._conn = sqlite3.connect(self._db_path, timeout=BUSY_TIMEOUT_SECONDS)
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
        conn.execute('BEGIN IMMEDIATE')
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
        for table, column, decl in _MIGRATIONS:
            if not self._has_column(table, column):
                self._add_column(table, column, decl)

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._get_conn().execute(f'PRAGMA table_info({table})').fetchall()
        return any(row['name'] == column for row in rows)

    def _add_column(self, table: str, column: str, decl: str) -> None:
        with self.transaction() as conn:
            if not self._has_column(table, column):
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')

    # --- helpers ---

    @staticmethod
    def _now() -> str:
        """Return current UTC time in ISO format."""
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    # --- Session operations ---

    def touch_session(self, session_id: str, cwd: str) -> None:
        """Create the session row if missing and mark it active now."""
        now = self._now()
        with self.transaction() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO sessions (session_id, started_at, cwd, last_active_at)'
                ' VALUES (?, ?, ?, ?)',
                (session_id, now, cwd, now),
            )
            conn.execute(
                'UPDATE sessions SET last_active_at = ? WHERE session_id = ?',
                (now, session_id),
            )

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

    def add_ack_if_allowed(
        self, session_id: str, item: str, reason: str,
        prompt_id: Optional[int] = None,
    ) -> str:
        """Add an ack in one write lock unless the item has an unconsumed ack or prompt_id is used.

        Returns 'added', 'stacked' or 'prompt_used'."""
        conn = self._get_conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            if conn.execute(
                'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL',
                (session_id, item),
            ).fetchone():
                conn.rollback()
                return 'stacked'
            if prompt_id is not None and conn.execute(
                'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND prompt_id = ?',
                (session_id, item, prompt_id),
            ).fetchone():
                conn.rollback()
                return 'prompt_used'
            conn.execute(
                'INSERT INTO acks (session_id, item, reason, created_at, prompt_id)'
                ' VALUES (?, ?, ?, ?, ?)',
                (session_id, item, reason, self._now(), prompt_id),
            )
            conn.commit()
            return 'added'
        except Exception:
            conn.rollback()
            raise

    def has_unconsumed_ack(self, session_id: str, item: str) -> bool:
        """Return True if at least one unconsumed ack exists for the item."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL',
            (session_id, item),
        ).fetchone()
        return row is not None

    def consume_acks(
        self, session_id: str, items: List[str],
        grants: Optional[List[tuple]] = None,
    ) -> bool:
        """Atomically consume one ack per item and record (item, prompt_id, reason) grants as used.

        Returns False and changes nothing if an item lacks an ack or a grant's prompt is used."""
        conn = self._get_conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            for item, prompt_id, _ in grants or []:
                used = conn.execute(
                    'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND prompt_id = ?',
                    (session_id, item, prompt_id),
                ).fetchone()
                if used:
                    conn.rollback()
                    return False
            chosen: List[int] = []
            for item in items:
                rows = conn.execute(
                    'SELECT id FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL '
                    'ORDER BY id ASC',
                    (session_id, item),
                ).fetchall()
                ack_id = next((r['id'] for r in rows if r['id'] not in chosen), None)
                if ack_id is None:
                    conn.rollback()
                    return False
                chosen.append(ack_id)
            now = self._now()
            conn.executemany(
                'UPDATE acks SET consumed_at = ? WHERE id = ?', [(now, i) for i in chosen]
            )
            conn.executemany(
                'INSERT INTO acks (session_id, item, reason, created_at, consumed_at, prompt_id)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                [(session_id, item, reason, now, now, pid) for item, pid, reason in grants or []],
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    # --- UserPrompt operations ---

    def add_user_prompt(self, session_id: str, prompt: str) -> int:
        """Insert a user prompt, mark the session active, and return the prompt id."""
        now = self._now()
        with self.transaction() as conn:
            cursor = conn.execute(
                'INSERT INTO user_prompts (session_id, prompt, created_at) VALUES (?, ?, ?)',
                (session_id, prompt, now),
            )
            conn.execute(
                'INSERT OR IGNORE INTO sessions (session_id, started_at, cwd, last_active_at)'
                " VALUES (?, ?, '', ?)",
                (session_id, now, now),
            )
            conn.execute(
                'UPDATE sessions SET last_active_at = ? WHERE session_id = ?',
                (now, session_id),
            )
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

    def get_recent_prompts(self, session_id: str, limit: int) -> List[UserPrompt]:
        """Return the last `limit` prompts of the session, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            'SELECT id, session_id, prompt, created_at FROM user_prompts '
            'WHERE session_id = ? ORDER BY id DESC LIMIT ?',
            (session_id, limit),
        ).fetchall()
        return [
            UserPrompt(
                id=row['id'],
                session_id=row['session_id'],
                prompt=row['prompt'],
                created_at=row['created_at'],
            )
            for row in rows
        ]

    def has_ack_for_prompt(self, session_id: str, item: str, prompt_id: int) -> bool:
        """Return True if any ack (consumed or not) for the item cites prompt_id."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT 1 FROM acks WHERE session_id = ? AND item = ? AND prompt_id = ?',
            (session_id, item, prompt_id),
        ).fetchone()
        return row is not None

    def expire_prompt_acks_before(
        self, session_id: str, item: str, oldest_valid_prompt_id: Optional[int]
    ) -> None:
        """Mark unconsumed acks of prompts older than the given id as used (None: all of them)."""
        conn = self._get_conn()
        conn.execute(
            'UPDATE acks SET consumed_at = ? WHERE session_id = ? AND item = ? '
            'AND consumed_at IS NULL AND prompt_id IS NOT NULL '
            'AND (? IS NULL OR prompt_id < ?)',
            (self._now(), session_id, item, oldest_valid_prompt_id, oldest_valid_prompt_id),
        )
        conn.commit()

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

    # --- NudgeFiring operations ---

    def get_latest_prompt_id(self, session_id: str) -> Optional[int]:
        """Return the latest prompt_id for this session, or None if no prompts yet."""
        conn = self._get_conn()
        cursor = conn.execute(
            'SELECT MAX(id) FROM user_prompts WHERE session_id = ?',
            (session_id,),
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else None

    def was_nudge_fired_this_turn(
        self, session_id: str, nudge_name: str, current_prompt_id: int
    ) -> bool:
        """Return True if this nudge was already fired during the current turn."""
        conn = self._get_conn()
        cursor = conn.execute(
            'SELECT prompt_id FROM nudge_firings WHERE session_id = ? AND nudge_name = ?',
            (session_id, nudge_name),
        )
        row = cursor.fetchone()
        return row is not None and row[0] == current_prompt_id

    def record_nudge_firing(
        self, session_id: str, nudge_name: str, prompt_id: int
    ) -> None:
        """Record (or update) the prompt_id at which this nudge last fired.

        One row per (session_id, nudge_name) is maintained via INSERT OR REPLACE.
        Each new turn overwrites the previous prompt_id, so was_nudge_fired_this_turn
        returns False on the next turn, allowing the nudge to fire again.
        """
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO nudge_firings (session_id, nudge_name, prompt_id)'
            ' VALUES (?, ?, ?)',
            (session_id, nudge_name, prompt_id),
        )
        conn.commit()

    # --- GatePass operations ---

    def record_gate_pass(self, session_id: str, gate_name: str, prompt_id: int) -> None:
        """Record the prompt_id of the turn in which the gate last passed."""
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO gate_passes (session_id, gate_name, prompt_id)'
            ' VALUES (?, ?, ?)',
            (session_id, gate_name, prompt_id),
        )
        conn.commit()

    def get_gate_pass_prompt_id(self, session_id: str, gate_name: str) -> Optional[int]:
        """Return the prompt_id of the turn in which the gate last passed, or None."""
        conn = self._get_conn()
        row = conn.execute(
            'SELECT prompt_id FROM gate_passes WHERE session_id = ? AND gate_name = ?',
            (session_id, gate_name),
        ).fetchone()
        return row['prompt_id'] if row else None

    # --- PendingEffect operations ---

    def add_pending(
        self, session_id: str, tool_use_id: str, kind: str, name: str, payload: dict
    ) -> None:
        """Hold a write until PostToolUse confirms that the tool call ran."""
        conn = self._get_conn()
        conn.execute(
            'INSERT OR REPLACE INTO pending_effects'
            ' (session_id, tool_use_id, kind, name, payload, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            (session_id, tool_use_id, kind, name, json.dumps(payload), self._now()),
        )
        conn.commit()

    def has_pending(self, session_id: str, tool_use_id: str) -> bool:
        row = self._get_conn().execute(
            'SELECT 1 FROM pending_effects WHERE session_id = ? AND tool_use_id = ?',
            (session_id, tool_use_id),
        ).fetchone()
        return row is not None

    def take_pending(self, session_id: str, tool_use_id: str) -> List[PendingEffect]:
        """Remove and return the call's held writes; a second call gets none."""
        with self.transaction() as conn:
            rows = conn.execute(
                'SELECT kind, name, payload FROM pending_effects'
                ' WHERE session_id = ? AND tool_use_id = ? ORDER BY created_at, kind, name',
                (session_id, tool_use_id),
            ).fetchall()
            conn.execute(
                'DELETE FROM pending_effects WHERE session_id = ? AND tool_use_id = ?',
                (session_id, tool_use_id),
            )
        return [PendingEffect(r['kind'], r['name'], json.loads(r['payload'])) for r in rows]

    # --- Housekeeping ---

    def cleanup(self, now: Optional[datetime.datetime] = None) -> Dict[str, int]:
        """Delete aged-out rows in one transaction; returns deleted counts per table."""
        now = now or datetime.datetime.now(datetime.timezone.utc)

        def cutoff(days: int) -> str:
            return (now - datetime.timedelta(days=days)).isoformat()

        last_active = 'COALESCE(last_active_at, started_at)'
        deleted: Dict[str, int] = {}
        with self.transaction() as conn:
            for table, days in _RETENTION_DAYS.items():
                deleted[table] = conn.execute(
                    f'DELETE FROM {table} WHERE session_id IN '
                    f'(SELECT session_id FROM sessions WHERE {last_active} < ?)',
                    (cutoff(days),),
                ).rowcount
            for table, days in _RETENTION_DAYS.items():
                column = _ORPHAN_TIME_COLUMN.get(table)
                if column:
                    sql = (
                        f'DELETE FROM {table} WHERE {column} < ? '
                        'AND session_id NOT IN (SELECT session_id FROM sessions)'
                    )
                    params: tuple = (cutoff(days),)
                else:
                    sql = (
                        f'DELETE FROM {table} WHERE session_id NOT IN '
                        '(SELECT session_id FROM sessions) AND session_id NOT IN '
                        '(SELECT session_id FROM user_prompts)'
                    )
                    params = ()
                deleted[table] += conn.execute(sql, params).rowcount
            deleted['sessions'] = conn.execute(
                f'DELETE FROM sessions WHERE {last_active} < ?',
                (cutoff(max(_RETENTION_DAYS.values())),),
            ).rowcount
        return deleted
