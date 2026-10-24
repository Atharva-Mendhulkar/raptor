"""MCP Streamable HTTP mock tool server with an append-only effect ledger."""
from __future__ import annotations

import json
from typing import Optional
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from raptor.server.ledger_db import LedgerDB


def create_mock_mcp_server(
    server_name: str = "raptor-mock-bank",
    ledger: Optional[LedgerDB] = None,
) -> tuple[MCPServer, LedgerDB]:
    """Create an MCPServer configured with non-idempotent effectful tools."""
    if ledger is None:
        ledger = LedgerDB()

    server = MCPServer(server_name)

    @server.tool(
        name="transfer",
        description="Transfer funds to a recipient. Effectful, non-idempotent operation.",
    )
    def transfer(amount: float, recipient: str, memo: str) -> str:
        """Executes a financial transfer and records it to the ledger."""
        record = ledger.record_effect(
            tool_name="transfer",
            logical_op_id=memo,
            recipient=recipient,
            amount=amount,
            meta={"transfer_type": "standard_wire"},
        )
        return json.dumps({
            "status": "COMMITTED",
            "effect_id": record.id,
            "logical_op_id": memo,
            "recipient": recipient,
            "amount": amount,
            "timestamp": record.timestamp,
        })

    @server.tool(
        name="get_transfer_status",
        description="Check status of a transfer by memo/logical reference. Read-only query.",
    )
    def get_transfer_status(memo: str) -> str:
        """Read-back tool to check if an effect was committed."""
        effects = ledger.get_effects_for_op(memo)
        if effects:
            return json.dumps({
                "found": True,
                "count": len(effects),
                "effects": [e.model_dump() for e in effects],
            })
        return json.dumps({"found": False, "count": 0})

    @server.tool(
        name="probe_unknown_outcome",
        description="Tool that deliberately returns isError: true with an UNKNOWN_OUTCOME code to test client retry behavior.",
    )
    def probe_unknown_outcome(memo: str) -> str:
        """Returns structured error to test whether agent framework retries."""
        payload = json.dumps({
            "isError": True,
            "error_code": "UNKNOWN_OUTCOME",
            "message": "Connection to upstream timed out after commit window; outcome unknown",
            "logical_op_id": memo,
            "recommended_action": "call get_transfer_status",
        })
        raise ToolError(payload)

    return server, ledger
