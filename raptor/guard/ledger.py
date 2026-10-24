"""
Durable Guard Effect Ledger using SQLite WAL mode.
Fulfills Requirement G4:
'Durable effect ledger with states NEW, IN_FLIGHT, COMMITTED, FAILED_DEFINITE, UNKNOWN.
After crash/restart, no COMMITTED key is forwarded again; IN_FLIGHT keys restart as UNKNOWN.'
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, Optional
from pydantic import BaseModel

from raptor.common.models import LedgerState


class GuardLedgerEntry(BaseModel):
    idempotency_key: str
    tool_name: str
    state: LedgerState
    stored_response_json: Optional[str] = None
    created_at: float
    updated_at: float


class GuardLedger:
    """Thread-safe and process-safe SQLite WAL ledger for the RAPTOR Guard Proxy."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None or db_path == ":memory:":
            self._db_path = f"file:guard_ledger_{id(self)}_{time.time_ns()}?mode=memory&cache=shared"
            self._is_uri = True
            # Keep a persistent connection to keep in-memory database alive
            self._keepalive = sqlite3.connect(self._db_path, uri=True)
        else:
            self._db_path = db_path
            self._is_uri = db_path.startswith("file:")
            self._keepalive = None

        self._init_db()
        self.recover_crashed_in_flight()

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_uri:
            conn = sqlite3.connect(self._db_path, uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guard_ledger (
                    idempotency_key TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stored_response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_guard_state
                ON guard_ledger(state);
                """
            )
            conn.commit()

    def recover_crashed_in_flight(self) -> int:
        """
        Crash recovery: Any key that was left IN_FLIGHT when the process terminated
        is safely transitioned to UNKNOWN state so retries observe explicit unknown outcome
        and do not hang or silently re-execute blindly.
        """
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE guard_ledger
                SET state = 'UNKNOWN', updated_at = ?
                WHERE state = 'IN_FLIGHT';
                """,
                (now,),
            )
            conn.commit()
            return cur.rowcount

    def get_entry(self, idempotency_key: str) -> Optional[GuardLedgerEntry]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT idempotency_key, tool_name, state, stored_response_json, created_at, updated_at "
                "FROM guard_ledger WHERE idempotency_key = ?;",
                (idempotency_key,),
            ).fetchone()
            if not row:
                return None
            return GuardLedgerEntry(
                idempotency_key=row["idempotency_key"],
                tool_name=row["tool_name"],
                state=LedgerState(row["state"]),
                stored_response_json=row["stored_response_json"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def mark_in_flight(self, idempotency_key: str, tool_name: str) -> bool:
        """
        Attempts to register the key as IN_FLIGHT.
        Returns True if newly created or successfully transitioned to IN_FLIGHT.
        Returns False if the key already exists and is not allowed to transition (e.g. COMMITTED).
        """
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT state FROM guard_ledger WHERE idempotency_key = ?;",
                (idempotency_key,),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO guard_ledger (idempotency_key, tool_name, state, created_at, updated_at)
                    VALUES (?, ?, 'IN_FLIGHT', ?, ?);
                    """,
                    (idempotency_key, tool_name, now, now),
                )
                conn.commit()
                return True

            state = LedgerState(row["state"])
            if state in (LedgerState.COMMITTED, LedgerState.IN_FLIGHT, LedgerState.UNKNOWN):
                return False

            if state == LedgerState.FAILED_DEFINITE:
                conn.execute(
                    """
                    UPDATE guard_ledger
                    SET state = 'IN_FLIGHT', updated_at = ?
                    WHERE idempotency_key = ?;
                    """,
                    (now, idempotency_key),
                )
                conn.commit()
                return True

            return False

    def mark_committed(self, idempotency_key: str, response_json: str, tool_name: str = "tool") -> None:
        """Transitions state to COMMITTED and stores the response payload for replay."""
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE guard_ledger
                SET state = 'COMMITTED', stored_response_json = ?, updated_at = ?
                WHERE idempotency_key = ?;
                """,
                (response_json, now, idempotency_key),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO guard_ledger (idempotency_key, tool_name, state, stored_response_json, created_at, updated_at)
                    VALUES (?, ?, 'COMMITTED', ?, ?, ?);
                    """,
                    (idempotency_key, tool_name, response_json, now, now),
                )
            conn.commit()

    def mark_failed_definite(self, idempotency_key: str, tool_name: str = "tool") -> None:
        """Transitions state to FAILED_DEFINITE (e.g. tool validation error)."""
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE guard_ledger
                SET state = 'FAILED_DEFINITE', updated_at = ?
                WHERE idempotency_key = ?;
                """,
                (now, idempotency_key),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO guard_ledger (idempotency_key, tool_name, state, created_at, updated_at)
                    VALUES (?, ?, 'FAILED_DEFINITE', ?, ?);
                    """,
                    (idempotency_key, tool_name, now, now),
                )
            conn.commit()

    def mark_unknown(self, idempotency_key: str, tool_name: str = "tool") -> None:
        """Transitions state to UNKNOWN (e.g. dropped connection, upstream timeout)."""
        now = time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE guard_ledger
                SET state = 'UNKNOWN', updated_at = ?
                WHERE idempotency_key = ?;
                """,
                (now, idempotency_key),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    INSERT INTO guard_ledger (idempotency_key, tool_name, state, created_at, updated_at)
                    VALUES (?, ?, 'UNKNOWN', ?, ?);
                    """,
                    (idempotency_key, tool_name, now, now),
                )
            conn.commit()
