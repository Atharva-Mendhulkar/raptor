"""Probe for testing how client stacks handle isError: true vs protocol errors."""
from __future__ import annotations

import logging
from typing import Any, Dict
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)


class ToolErrorBehaviorProbe:
    """
    Empirical probe to verify whether stock MCP clients and frameworks
    auto-retry or crash when a tool returns isError: true vs JSON-RPC errors.
    """

    def __init__(self, endpoint_url: str):
        self.endpoint_url = endpoint_url

    async def probe_is_error_response(self, memo: str) -> Dict[str, Any]:
        """
        Calls probe_unknown_outcome tool which returns isError: true.
        Determines if ClientSession raises an exception or returns the error object to caller.
        """
        outcome = {
            "raised_exception": False,
            "exception_type": None,
            "exception_message": None,
            "result_returned": False,
            "is_error_flag": None,
            "content": None,
        }

        try:
            async with streamable_http_client(self.endpoint_url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "probe_unknown_outcome",
                        {"memo": memo},
                    )
                    outcome["result_returned"] = True
                    outcome["is_error_flag"] = getattr(result, "is_error", getattr(result, "isError", None))
                    outcome["content"] = [c.text for c in result.content if hasattr(c, "text")]
        except Exception as exc:
            outcome["raised_exception"] = True
            outcome["exception_type"] = type(exc).__name__
            outcome["exception_message"] = str(exc)

        return outcome
