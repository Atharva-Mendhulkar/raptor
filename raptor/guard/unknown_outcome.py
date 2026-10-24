"""
Standardized UNKNOWN_OUTCOME result constructor for MCP protocol.
Fulfills Requirement G6:
'Explicit unknown-outcome result with a machine-readable code for MCP.
Wire encoding documented; every adapter handles it without crashing.'
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def build_unknown_outcome_mcp_response(
    request_id: Any,
    tool_name: str,
    idempotency_key: str,
    status_check_tool: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Constructs an explicit MCP 2.x JSON-RPC response signaling UNKNOWN_OUTCOME with isError=True.
    Does NOT raise a transport error; safely delivers machine-readable state to the agent loop.
    """
    message_text = (
        f"UNKNOWN_OUTCOME: Backend tool call for '{tool_name}' timed out or was severed mid-flight. "
        f"Execution state is uncertain. Do not reissue blind retries with new parameters."
    )
    if status_check_tool:
        message_text += f" Query '{status_check_tool}' to verify whether effect was committed."
    if detail:
        message_text += f" Underlying error: {detail}"

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": message_text,
                }
            ],
            "isError": True,
            "_meta": {
                "raptor_status": "UNKNOWN_OUTCOME",
                "idempotency_key": idempotency_key,
                "tool_name": tool_name,
                "status_check_tool": status_check_tool,
                "retry_safe": False,
            },
        },
    }
