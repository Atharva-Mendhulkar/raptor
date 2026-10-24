"""
RAPTOR Gate G0 Minimal Standalone Harness.
1. Append-only SQLite ledger
2. MCP JSON-RPC mock server
3. Zero-dependency ASGI post-commit drop proxy
4. Deterministic G0 runner comparing SDK vs Framework re-plan
"""
import asyncio
import json
import sqlite3
import time
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


# --- 1. Ground-Truth SQLite Ledger ---
class Ledger:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE committed (id INTEGER PRIMARY KEY, memo TEXT, amount REAL, recipient TEXT, ts REAL)"
        )

    def commit(self, memo: str, amount: float, recipient: str):
        self.conn.execute(
            "INSERT INTO committed (memo, amount, recipient, ts) VALUES (?, ?, ?, ?)",
            (memo, amount, recipient, time.time()),
        )
        self.conn.commit()

    def count(self, memo: str) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM committed WHERE memo = ?", (memo,))
        return cur.fetchone()[0]


# --- 2. Mock MCP Server & Post-Commit Fault Proxy ---
ledger = Ledger()
fault_active = False


async def mcp_endpoint(request):
    data = await request.json()
    method = data.get("method")
    req_id = data.get("id")

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}}}
        }, headers={"mcp-session-id": "g0-sess"})

    if method == "tools/call":
        args = data.get("params", {}).get("arguments", {})
        memo = args.get("memo", "")
        amount = args.get("amount", 0.0)
        recipient = args.get("recipient", "")
        
        # Ground-truth commit
        ledger.commit(memo, amount, recipient)
        
        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": f"Transferred {amount} to {recipient}"}]}
        })

    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})


_base_app = Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])])


async def post_commit_drop_proxy(scope, receive, send):
    """Cuts TCP connection only AFTER the tool call has executed and committed."""
    if scope["type"] == "http":
        async def intercepting_send(msg):
            if fault_active and msg.get("type") == "http.response.start":
                raise ConnectionResetError("Connection lost after server-side commit")
            await send(msg)
        await _base_app(scope, receive, intercepting_send)
    else:
        await _base_app(scope, receive, send)


# --- 3. Client & Framework Runners ---
async def call_tool_sdk(client: httpx.AsyncClient, url: str, memo: str) -> bool:
    """Official SDK behavior: single request, raises on disconnect, no auto-retry."""
    payload = {
        "jsonrpc": "2.0", "id": f"req_{memo}",
        "method": "tools/call",
        "params": {"name": "transfer", "arguments": {"amount": 50.0, "recipient": "Target", "memo": memo}}
    }
    try:
        r = await client.post(url, json=payload, headers={"mcp-session-id": "g0-sess"})
        return r.status_code == 200
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError):
        return False


async def call_tool_framework(client: httpx.AsyncClient, url: str, memo: str, max_replans: int = 1) -> bool:
    """Agent Framework loop (LangGraph / ReAct): catches tool error and re-issues."""
    for attempt in range(max_replans + 1):
        success = await call_tool_sdk(client, url, memo)
        if success:
            return True
    return False


# --- 4. G0 Runner ---
async def run_g0(n_trials: int = 30):
    global fault_active
    port = 8979
    config = uvicorn.Config(post_commit_drop_proxy, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    srv_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)

    url = f"http://127.0.0.1:{port}/mcp"

    async with httpx.AsyncClient(timeout=1.0) as client:
        # Initialize
        await client.post(url, json={"jsonrpc": "2.0", "id": "init", "method": "initialize"})

        configs = [
            ("Control (no fault)", False, "sdk"),
            ("SDK (response lost)", True, "sdk"),
            ("Framework Re-Plan (response lost)", True, "framework"),
        ]

        print(f"\n{'Config':<36} | {'Success':<8} | {'Phantom':<8} | {'Duplicate':<10} | {'Multiplicity'}")
        print("-" * 80)

        for name, fault, stack_type in configs:
            fault_active = fault
            successes, phantoms, duplicates, total_commits = 0, 0, 0, 0

            for i in range(n_trials):
                memo = f"{name[:3]}_{i}"
                if stack_type == "sdk":
                    ok = await call_tool_sdk(client, url, memo)
                else:
                    ok = await call_tool_framework(client, url, memo, max_replans=1)

                commits = ledger.count(memo)
                total_commits += commits
                if ok: successes += 1
                if not ok and commits > 0: phantoms += 1
                if commits > 1: duplicates += 1

            mult = total_commits / n_trials
            print(f"{name:<36} | {successes}/{n_trials:<6} | {phantoms}/{n_trials:<6} | {duplicates}/{n_trials:<8} | {mult:.2f}")

    server.should_exit = True
    await srv_task


if __name__ == "__main__":
    asyncio.run(run_g0(n_trials=30))
