"""RAPTOR common data models and types."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class LedgerState(str, Enum):
    NEW = "NEW"
    IN_FLIGHT = "IN_FLIGHT"
    COMMITTED = "COMMITTED"
    FAILED_DEFINITE = "FAILED_DEFINITE"
    UNKNOWN = "UNKNOWN"


class ExecutionRecord(BaseModel):
    """Ground-truth record of a committed effect in the backend ledger."""
    id: Optional[int] = None
    timestamp: float
    tool_name: str
    logical_op_id: str
    recipient: str
    amount: float
    request_id: Optional[str] = None
    meta_json: str = "{}"


class AttemptRecord(BaseModel):
    """Record of a request attempt at proxy or client level."""
    attempt_id: str
    logical_op_id: str
    layer: str  # "transport", "sdk", "framework", "llm"
    timestamp: float
    request_id: Optional[str] = None
    status_code: Optional[int] = None
    outcome: str  # "SUCCESS", "ERROR", "DROPPED", "RESET"


class ToolPolicy(BaseModel):
    """Policy governing tool effectfulness, key derivation, and dedup window."""
    tool_name: str
    is_effectful: bool = True
    idempotency_fields: List[str] = field(default_factory=lambda: ["recipient", "amount"])
    dedup_window_seconds: float = 60.0
    scope: str = "default"
    status_check_tool: Optional[str] = None
    allow_retry: bool = True
    min_service_time_seconds: float = 0.05
