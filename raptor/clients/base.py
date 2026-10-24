"""Base client adapter interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple
from pydantic import BaseModel


class ClientRunResult(BaseModel):
    success: bool
    response_text: Optional[str] = None
    error_message: Optional[str] = None
    attempts_made: int
    meta: Dict[str, Any] = {}


class BaseClientAdapter(ABC):
    """Abstract base class for agent stack adapters under test."""

    def __init__(self, endpoint_url: str):
        self.endpoint_url = endpoint_url

    @abstractmethod
    async def execute_transfer(
        self,
        amount: float,
        recipient: str,
        memo: str,
        idempotency_key: Optional[str] = None,
    ) -> ClientRunResult:
        """Execute a transfer through the MCP tool interface."""
        pass
