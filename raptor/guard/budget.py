"""
RAPTOR Retry Budget and Deadline Enforcement.
Fulfills Requirements G8 and G9:
- G8: Token bucket retry budget per backend. Retries spend 1 token; first attempts accrue tokens.
- G9: Absolute deadline propagation. Rejects when remaining time < min_service_time.
"""
from __future__ import annotations

import time
from typing import Optional, Tuple


class TokenBucketRetryBudget:
    """
    Token bucket retry budget.
    Tokens accrue as a fraction of first attempts (token_ratio).
    Each retry consumes exactly 1 token.
    """

    def __init__(
        self,
        token_ratio: float = 0.2,
        burst: float = 5.0,
        max_tokens: float = 50.0,
    ):
        self.token_ratio = token_ratio
        self.burst = burst
        self.tokens = burst
        self.max_tokens = max_tokens

    def record_first_attempt(self) -> None:
        """Accrue tokens as a fraction of a first attempt."""
        self.tokens = min(self.max_tokens, self.tokens + self.token_ratio)

    def can_retry(self) -> bool:
        """Check if at least one token is available."""
        return self.tokens >= 1.0

    def consume_retry(self) -> bool:
        """Spend one retry token if available."""
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def check_deadline(
    deadline_epoch: Optional[float],
    min_service_time: float = 0.05,
    now: Optional[float] = None,
) -> Tuple[bool, float]:
    """
    Checks absolute deadline against current time and tool minimum service time.
    Returns (is_valid, remaining_seconds).
    """
    if deadline_epoch is None:
        return True, float("inf")
    current_time = now if now is not None else time.time()
    remaining = deadline_epoch - current_time
    if remaining < min_service_time:
        return False, remaining
    return True, remaining
