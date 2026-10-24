"""SQLite append-only ground-truth effect log for tool servers."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from raptor.common.models import ExecutionRecord


class LedgerDB:
    """Thread-safe, append-only SQLite database for logging committed effects."""

    def __init__(self, db_path: str | Path = ":memory:"):
        self.raw_path = str(db_path)
        if self.raw_path == ":memory:":
            # Use shared in-memory URI with unique name per instance
            self._uri = f"file:raptor_ledger_{id(self)}_{time.time_ns()}?mode=memory&cache=shared"
            self._is_uri = True
            # Keep a persistent connection to keep in-memory database alive
            self._keepalive = sqlite3.connect(self._uri, uri=True)
        else:
            self._uri = self.raw_path
            self._is_uri = False
            self._keepalive = None

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_uri:
            conn = sqlite3.connect(self._uri, uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(self._uri, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS committed_effects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    tool_name TEXT NOT NULL,
                    logical_op_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    amount REAL NOT NULL,
                    request_id TEXT,
                    meta_json TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_logical_op_id 
                ON committed_effects(logical_op_id);
                """
            )
            conn.commit()

    def record_effect(
        self,
        tool_name: str,
        logical_op_id: str,
        recipient: str,
        amount: float,
        request_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ExecutionRecord:
        """Atomically append a committed side effect to the ground-truth log."""
        ts = time.time()
        meta_str = json.dumps(meta or {})
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO committed_effects 
                (timestamp, tool_name, logical_op_id, recipient, amount, request_id, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (ts, tool_name, logical_op_id, recipient, amount, request_id, meta_str),
            )
            record_id = cursor.lastrowid
            conn.commit()

        return ExecutionRecord(
            id=record_id,
            timestamp=ts,
            tool_name=tool_name,
            logical_op_id=logical_op_id,
            recipient=recipient,
            amount=amount,
            request_id=request_id,
            meta_json=meta_str,
        )

    def count_effects_for_op(self, logical_op_id: str) -> int:
        """Count the number of committed executions for a given logical operation id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM committed_effects WHERE logical_op_id = ?;",
                (logical_op_id,),
            )
            return cursor.fetchone()[0]

    def get_effects_for_op(self, logical_op_id: str) -> List[ExecutionRecord]:
        """Retrieve all committed executions for a given logical operation id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, timestamp, tool_name, logical_op_id, recipient, amount, request_id, meta_json
                FROM committed_effects WHERE logical_op_id = ?
                ORDER BY id ASC;
                """,
                (logical_op_id,),
            )
            rows = cursor.fetchall()
            return [
                ExecutionRecord(
                    id=row["id"],
                    timestamp=row["timestamp"],
                    tool_name=row["tool_name"],
                    logical_op_id=row["logical_op_id"],
                    recipient=row["recipient"],
                    amount=row["amount"],
                    request_id=row["request_id"],
                    meta_json=row["meta_json"],
                )
                for row in rows
            ]

    def get_all_effects(self) -> List[ExecutionRecord]:
        """Retrieve all committed executions across all operations."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, timestamp, tool_name, logical_op_id, recipient, amount, request_id, meta_json
                FROM committed_effects
                ORDER BY id ASC;
                """
            )
            rows = cursor.fetchall()
            return [
                ExecutionRecord(
                    id=row["id"],
                    timestamp=row["timestamp"],
                    tool_name=row["tool_name"],
                    logical_op_id=row["logical_op_id"],
                    recipient=row["recipient"],
                    amount=row["amount"],
                    request_id=row["request_id"],
                    meta_json=row["meta_json"],
                )
                for row in rows
            ]

    def clear(self) -> None:
        """Clear all records (for test isolation)."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM committed_effects;")
            conn.commit()
