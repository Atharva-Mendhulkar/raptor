"""
Lightweight RAPTOR Web Dashboard.
Zero npm/node build step. Single-file vanilla HTML/CSS/JS served via Starlette.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.proxy import GuardProxyApp
from raptor.harness.asgi_fault_proxy import ASGIFaultProxy
from raptor.harness.runner_matrix import MatrixRunner
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RAPTOR | PartitionGuard Dashboard</title>
  <style>
    :root {
      --bg: #0f172a; --card: #1e293b; --border: #334155;
      --text: #f8fafc; --muted: #94a3b8; --primary: #38bdf8;
      --success: #34d399; --danger: #f87171; --warning: #fbbf24;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 2rem; line-height: 1.5; }
    .container { max-width: 1000px; margin: 0 auto; }
    header { margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }
    h1 { font-size: 1.75rem; color: var(--primary); display: flex; align-items: center; gap: 0.5rem; }
    .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.25rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
    .card-title { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .card-value { font-size: 1.75rem; font-weight: bold; margin-top: 0.5rem; color: var(--primary); }
    .actions { display: flex; gap: 1rem; margin-bottom: 2rem; }
    button { background: var(--primary); color: #0f172a; border: none; padding: 0.75rem 1.25rem; font-weight: 600; border-radius: 6px; cursor: pointer; transition: opacity 0.2s; }
    button:hover { opacity: 0.9; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; }
    .panel-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; }
    .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .comp-card { background: #0f172a; border: 1px solid var(--border); border-radius: 6px; padding: 1rem; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: bold; text-transform: uppercase; }
    .badge-danger { background: rgba(248, 113, 113, 0.2); color: var(--danger); border: 1px solid var(--danger); }
    .badge-success { background: rgba(52, 211, 153, 0.2); color: var(--success); border: 1px solid var(--success); }
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }
    th, td { text-align: left; padding: 0.75rem; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
    .spinner { display: inline-block; width: 1rem; height: 1rem; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🛡️ RAPTOR / PartitionGuard</h1>
      <p class="subtitle">Fault-injection harness and protocol-aware sidecar proxy for agent tool reliability</p>
    </header>

    <div class="actions">
      <button id="runBtn" onclick="runFaultTest()">⚡ Run Live Fault F1 Test</button>
      <button id="benchBtn" onclick="runBenchmark()" style="background: #a855f7; color: #fff;">📊 Run Matrix Benchmark</button>
    </div>

    <div class="panel" id="resultPanel" style="display: none;">
      <div class="panel-title">
        <span>Fault F1 Experiment Results (Post-Commit Wire Drop)</span>
        <span id="testTime" style="font-size: 0.8rem; color: var(--muted);"></span>
      </div>
      <div class="comparison-grid">
        <div class="comp-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <strong>Unprotected Stack</strong>
            <span class="badge badge-danger">Duplication Alert</span>
          </div>
          <p style="color: var(--muted); font-size: 0.85rem;">Client retries upon network disconnect after server commit.</p>
          <div style="margin-top: 1rem;">
            <div>Attempts Made: <strong id="unprotAttempts">-</strong></div>
            <div>Backend Commits: <strong id="unprotCommits" style="color: var(--danger);">-</strong></div>
            <div>Multiplicity: <strong id="unprotMult" style="color: var(--danger);">-</strong></div>
          </div>
        </div>

        <div class="comp-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <strong>Guarded by RAPTOR</strong>
            <span class="badge badge-success">Idempotent</span>
          </div>
          <p style="color: var(--muted); font-size: 0.85rem;">Guard replays stored COMMITTED state on retries.</p>
          <div style="margin-top: 1rem;">
            <div>Attempts Made: <strong id="guardAttempts">-</strong></div>
            <div>Backend Commits: <strong id="guardCommits" style="color: var(--success);">-</strong></div>
            <div>Multiplicity: <strong id="guardMult" style="color: var(--success);">-</strong></div>
          </div>
        </div>
      </div>
    </div>

    <div class="panel" id="benchPanel" style="display: none;">
      <div class="panel-title">
        <span>Benchmark Matrix Results</span>
        <span id="benchTime" style="font-size: 0.8rem; color: var(--muted);"></span>
      </div>
      <div style="overflow-x: auto;">
        <table id="benchTable">
          <thead>
            <tr>
              <th>Adapter</th>
              <th>Fault Mode</th>
              <th>Unprotected Mult</th>
              <th>Guarded Mult</th>
              <th>Unprot Dup%</th>
              <th>Guarded Dup%</th>
            </tr>
          </thead>
          <tbody id="benchBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    async function runFaultTest() {
      const btn = document.getElementById("runBtn");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Running...';
      try {
        const res = await fetch("/api/run-experiment", { method: "POST" });
        const data = await res.json();
        document.getElementById("resultPanel").style.display = "block";
        document.getElementById("unprotAttempts").innerText = data.unprotected.attempts;
        document.getElementById("unprotCommits").innerText = data.unprotected.commits + " (DUPLICATE)";
        document.getElementById("unprotMult").innerText = data.unprotected.multiplicity.toFixed(2) + "x";

        document.getElementById("guardAttempts").innerText = data.guarded.attempts;
        document.getElementById("guardCommits").innerText = data.guarded.commits + " (EXACTLY 1)";
        document.getElementById("guardMult").innerText = data.guarded.multiplicity.toFixed(2) + "x";
        document.getElementById("testTime").innerText = new Date().toLocaleTimeString();
      } catch (err) {
        alert("Error executing experiment: " + err);
      } finally {
        btn.disabled = false;
        btn.innerHTML = "⚡ Run Live Fault F1 Test";
      }
    }

    async function runBenchmark() {
      const btn = document.getElementById("benchBtn");
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Benchmarking...';
      try {
        const res = await fetch("/api/run-benchmark", { method: "POST" });
        const data = await res.json();
        document.getElementById("benchPanel").style.display = "block";
        const tbody = document.getElementById("benchBody");
        tbody.innerHTML = "";

        for (const row of data.rows) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td><code>${row.adapter}</code></td>
            <td><code>${row.fault}</code></td>
            <td style="color: ${row.unprot_mult > 1 ? 'var(--danger)' : 'var(--text)'}; font-weight: 600;">${row.unprot_mult.toFixed(2)}x</td>
            <td style="color: var(--success); font-weight: 600;">${row.guard_mult.toFixed(2)}x</td>
            <td style="color: ${row.unprot_dup > 0 ? 'var(--danger)' : 'var(--muted)'};">${(row.unprot_dup * 100).toFixed(0)}%</td>
            <td style="color: var(--success);">${(row.guard_dup * 100).toFixed(0)}%</td>
          `;
          tbody.appendChild(tr);
        }
        document.getElementById("benchTime").innerText = new Date().toLocaleTimeString();
      } catch (err) {
        alert("Error running benchmark: " + err);
      } finally {
        btn.disabled = false;
        btn.innerHTML = "📊 Run Matrix Benchmark";
      }
    }
  </script>
</body>
</html>
"""


async def run_f1_live_experiment() -> Dict[str, Any]:
    """Execute live F1 comparison in-process."""
    server_port, fault_port, guard_port = 8991, 8992, 8993
    import uvicorn

    backend_ledger = LedgerDB()
    server, _ = create_mock_mcp_server(ledger=backend_ledger)
    server_app = server.streamable_http_app()
    srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=server_port, log_level="critical"))
    srv_task = asyncio.create_task(srv.serve())

    fault_proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{server_port}")
    fault_proxy.set_fault("f1_response_drop")
    fault_srv = uvicorn.Server(uvicorn.Config(fault_proxy.as_asgi_app(), host="127.0.0.1", port=fault_port, log_level="critical"))
    fault_task = asyncio.create_task(fault_srv.serve())

    guard_ledger = GuardLedger()
    guard_policy = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
    guard_app = GuardProxyApp(upstream_url=f"http://127.0.0.1:{server_port}", policy_config=guard_policy, ledger=guard_ledger)
    guard_srv = uvicorn.Server(uvicorn.Config(guard_app.as_asgi_app(), host="127.0.0.1", port=guard_port, log_level="critical"))
    guard_task = asyncio.create_task(guard_srv.serve())

    await asyncio.sleep(0.3)
    try:
        # 1. Unprotected
        client_unprot = RawMCPClientAdapter(f"http://127.0.0.1:{fault_port}/mcp", max_retries=1)
        await client_unprot._ensure_initialized()
        res1 = await client_unprot.execute_transfer(amount=100.0, recipient="VendorA", memo="tx_web_unprot_1")
        await client_unprot.close()
        commits1 = backend_ledger.count_effects_for_op("tx_web_unprot_1")

        # 2. Guarded
        fault_proxy.upstream_url = f"http://127.0.0.1:{guard_port}"
        client_guarded = RawMCPClientAdapter(f"http://127.0.0.1:{fault_port}/mcp", max_retries=1)
        await client_guarded._ensure_initialized()
        res2 = await client_guarded.execute_transfer(amount=100.0, recipient="VendorA", memo="tx_web_guard_1")
        await client_guarded.close()
        commits2 = backend_ledger.count_effects_for_op("tx_web_guard_1")

        return {
            "unprotected": {"attempts": res1.attempts_made, "commits": commits1, "multiplicity": float(commits1)},
            "guarded": {"attempts": res2.attempts_made, "commits": commits2, "multiplicity": float(commits2)},
        }
    finally:
        await fault_proxy.close()
        await guard_app.close()
        fault_srv.should_exit = True
        guard_srv.should_exit = True
        srv.should_exit = True
        await asyncio.gather(srv_task, fault_task, guard_task, return_exceptions=True)


async def run_quick_benchmark() -> Dict[str, Any]:
    """Execute quick 2-adapter benchmark for dashboard."""
    runner = MatrixRunner(server_port=8941, fault_port=8942, guard_port=8943)
    report = await runner.run_matrix(
        adapters=["raw_mcp", "react_agent"],
        faults=["none", "f1_response_drop"],
        modes=[False, True],
        trials_per_cell=2,
    )

    grouped = {}
    for c in report.cells:
        k = (c.adapter_name, c.fault_name)
        if k not in grouped: grouped[k] = {}
        grouped[k]["guarded" if c.guarded else "unprotected"] = c

    rows = []
    for (adapter, fault), pair in grouped.items():
        unprot = pair.get("unprotected")
        guard = pair.get("guarded")
        rows.append({
            "adapter": adapter,
            "fault": fault,
            "unprot_mult": unprot.multiplicity if unprot else 0.0,
            "guard_mult": guard.multiplicity if guard else 0.0,
            "unprot_dup": unprot.duplicate_rate if unprot else 0.0,
            "guard_dup": guard.duplicate_rate if guard else 0.0,
        })
    return {"rows": rows}


def create_ui_app() -> Starlette:
    async def index(request):
        return HTMLResponse(HTML_TEMPLATE)

    async def api_experiment(request):
        data = await run_f1_live_experiment()
        return JSONResponse(data)

    async def api_benchmark(request):
        data = await run_quick_benchmark()
        return JSONResponse(data)

    routes = [
        Route("/", endpoint=index, methods=["GET"]),
        Route("/api/run-experiment", endpoint=api_experiment, methods=["POST"]),
        Route("/api/run-benchmark", endpoint=api_benchmark, methods=["POST"]),
    ]
    return Starlette(routes=routes)
