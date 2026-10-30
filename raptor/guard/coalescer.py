"""
In-flight request coalescer (Singleflight pattern).
Fulfills Requirement G5:
'Coalescing of concurrent retries onto one upstream call.
With 2 to 20 parallel identical retries, the backend receives exactly one request.'
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class RequestCoalescer:
    """
    Ensures that for any given idempotency key, only one upstream execution runs concurrently.
    Concurrent calls with the same key wait for the in-flight execution and share its result.
    """

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def run(
        self,
        key: str,
        action: Callable[[], Any],
    ) -> Tuple[Any, bool]:
        """
        Generic singleflight coalescing returning (result, was_coalesced: bool).
        """
        async with self._lock:
            if key in self._in_flight:
                future = self._in_flight[key]
                is_leader = False
                logger.info(f"Coalescing request on key '{key}': waiting on active leader")
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._in_flight[key] = future
                is_leader = True

        if not is_leader:
            res = await future
            return res, True

        try:
            result = await action()
            if not future.done():
                future.set_result(result)
            return result, False
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._in_flight.pop(key, None)
            # Acknowledge future exception so asyncio doesn't warn if there were no followers
            if future.done() and not future.cancelled() and future.exception():
                pass

    async def execute(
        self,
        key: str,
        action: Callable[[], Awaitable[Tuple[int, bytes, Dict[str, str]]]],
    ) -> Tuple[int, bytes, Dict[str, str], bool]:
        """
        Executes HTTP action or joins an already in-flight execution for `key`.
        Returns (status_code, body_bytes, headers_dict, was_coalesced: bool).
        """
        res, was_coalesced = await self.run(key, action)
        return res[0], res[1], res[2], was_coalesced
