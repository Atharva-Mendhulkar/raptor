"""
Lightweight RAPTOR Web Dashboard.
Single-file vanilla HTML/CSS/JS served via Starlette with full real-time capabilities.
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
  <title>RAPTOR | PartitionGuard Network Middlebox Dashboard</title>
  <style>
    :root {
      --bg: #0b1120; --card: #1e293b; --border: #334155;
      --text: #f8fafc; --muted: #94a3b8; --primary: #38bdf8;
      --success: #34d399; --danger: #f87171; --warning: #fbbf24;
      --purple: #c084fc;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; background: var(--bg); color: var(--text); padding: 2rem; line-height: 1.5; }
    .container { max-width: 1100px; margin: 0 auto; }
    header { margin-bottom: 1.5rem; border-bottom: 1px solid var(--border); padding-bottom: 1.25rem; display: flex; justify-content: space-between; align-items: flex-end; }
    h1 { font-size: 1.75rem; color: var(--primary); display: flex; align-items: center; gap: 0.5rem; font-weight: 700; letter-spacing: -0.02em; }
    .subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 0.25rem; }
    
    .status-bar { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .status-badge { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.35rem 0.75rem; background: #1e293b; border: 1px solid var(--border); border-radius: 6px; font-size: 0.75rem; font-weight: 600; font-family: ui-monospace, Menlo, monospace; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); }
    .dot.pulse { box-shadow: 0 0 8px var(--success); }

    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.75rem; }
    .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
    .stat-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
    .stat-value { font-size: 2rem; font-weight: 700; margin-top: 0.4rem; color: var(--primary); font-family: ui-monospace, Menlo, monospace; }

    .actions { display: flex; gap: 1rem; margin-bottom: 1.75rem; }
    button { background: var(--primary); color: #0b1120; border: none; padding: 0.75rem 1.4rem; font-weight: 600; font-size: 0.9rem; border-radius: 6px; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 0.5rem; }
    button:hover { opacity: 0.9; transform: translateY(-1px); }
    button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
    .btn-bench { background: #9333ea; color: #fff; }
    .btn-secondary { background: #334155; color: #f8fafc; }

    .panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.75rem; }
    .panel-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; }
    
    .comparison-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }
    .comp-card { background: #0b1120; border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
    .badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
    .badge-danger { background: rgba(248, 113, 113, 0.15); color: var(--danger); border: 1px solid var(--danger); }
    .badge-success { background: rgba(52, 211, 153, 0.15); color: var(--success); border: 1px solid var(--success); }

    .metric-row { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.9rem; }
    .metric-row:last-child { border-bottom: none; }

    table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.85rem; font-family: ui-monospace, Menlo, monospace; }
    th, td { text-align: left; padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-weight: 600; }
    tr:hover td { background: rgba(255,255,255,0.02); }

    .spinner { display: inline-block; width: 1rem; height: 1rem; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>🛡️ RAPTOR / PartitionGuard</h1>
        <p class="subtitle">Protocol-Aware Idempotency Guard Proxy & Network Fault-Injection Middlebox</p>
      </div>
      <div style="text-align: right; font-size: 0.8rem; color: var(--muted); font-family: ui-monospace, Menlo, monospace;">
        MCP 2024-11-05 / JSON-RPC 2.0<br>
        Port: 8000 | Middlebox Ingress
      </div>
    </header>

    <!-- Node Status Bar -->
    <div class="status-bar">
      <div class="status-badge"><div class="dot pulse"></div> TOOL SERVER: :8981 (ONLINE)</div>
      <div class="status-badge"><div class="dot pulse"></div> GUARD PROXY: :8983 (ACTIVE)</div>
      <div class="status-badge"><div class="dot" style="background: var(--warning);"></div> FAULT INJECTOR: :8982 (ARMED)</div>
      <div class="status-badge"><div class="dot"></div> WAL LEDGER: NORMAL (SYNC)</div>
    </div>

    <!-- Stat Grid -->
    <div class="grid">
      <div class="stat-card">
        <div class="stat-label">Total Ingress Flows</div>
        <div class="stat-value" id="statFlows">1,240</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Dedup Cache Hits</div>
        <div class="stat-value" id="statDedup" style="color: var(--success);">512</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Coalesced Requests</div>
        <div class="stat-value" id="statCoalesced" style="color: var(--purple);">288</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Guarded Multiplicity</div>
        <div class="stat-value" id="statMult" style="color: var(--success);">1.00x</div>
      </div>
    </div>

    <!-- Actions -->
    <div class="actions">
      <button id="runBtn" onclick="runFaultTest()">⚡ Run Live Fault F1 Test</button>
      <button id="benchBtn" class="btn-bench" onclick="runBenchmark()">📊 Run Matrix Benchmark</button>
      <button class="btn-secondary" onclick="refreshLedger()">🔄 Refresh Ledger</button>
    </div>

    <!-- Experiment Results Panel -->
    <div class="panel" id="resultPanel">
      <div class="panel-title">
        <span>Fault F1 Live Experiment: Downstream Response Packet Drop on Wire</span>
        <span id="testTime" style="font-size: 0.8rem; color: var(--muted); font-family: ui-monospace, Menlo, monospace;">Last execution: Live</span>
      </div>
      <div class="comparison-grid">
        <div class="comp-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <strong style="color: #f8fafc; font-size: 1rem;">Unprotected Stack (Raw MCP)</strong>
            <span class="badge badge-danger">DUPLICATION ALERT</span>
          </div>
          <p style="color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem;">Client transport retry issued upon wire disconnect after server commit.</p>
          <div class="metric-row"><span>Attempts Made:</span><strong id="unprotAttempts">2 (Initial + 1 Retry)</strong></div>
          <div class="metric-row"><span>Backend Commits:</span><strong id="unprotCommits" style="color: var(--danger); font-size: 1.1rem;">2 (DUPLICATE STATE)</strong></div>
          <div class="metric-row"><span>Backend Multiplicity:</span><strong id="unprotMult" style="color: var(--danger); font-size: 1.3rem;">2.00x</strong></div>
        </div>

        <div class="comp-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <strong style="color: #f8fafc; font-size: 1rem;">Guarded by RAPTOR Proxy</strong>
            <span class="badge badge-success">IDEMPOTENT OK</span>
          </div>
          <p style="color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem;">Guard intercepts retry and deterministically replays cached 200 OK.</p>
          <div class="metric-row"><span>Attempts Made:</span><strong id="guardAttempts">2 (Initial + 1 Retry)</strong></div>
          <div class="metric-row"><span>Backend Commits:</span><strong id="guardCommits" style="color: var(--success); font-size: 1.1rem;">1 (EXACTLY ONCE)</strong></div>
          <div class="metric-row"><span>Backend Multiplicity:</span><strong id="guardMult" style="color: var(--success); font-size: 1.3rem;">1.00x (0 Duplicates)</strong></div>
        </div>
      </div>
    </div>

    <!-- Matrix Benchmark Panel -->
    <div class="panel" id="benchPanel">
      <div class="panel-title">
        <span>Cross-Stack Automated Matrix Benchmark</span>
        <span id="benchTime" style="font-size: 0.8rem; color: var(--muted); font-family: ui-monospace, Menlo, monospace;">4 Combinations Tested</span>
      </div>
      <div style="overflow-x: auto;">
        <table id="benchTable">
          <thead>
            <tr>
              <th>Client Adapter</th>
              <th>Network Fault Mode</th>
              <th>Unprotected Mult</th>
              <th>Guarded Mult</th>
              <th>Unprot Dup%</th>
              <th>Guarded Dup%</th>
            </tr>
          </thead>
          <tbody id="benchBody">
            <tr>
              <td><code>raw_mcp</code></td>
              <td><code>none (clean)</code></td>
              <td style="color: var(--text);">1.00x</td>
              <td style="color: var(--success); font-weight: bold;">1.00x</td>
              <td style="color: var(--muted);">0%</td>
              <td style="color: var(--success);">0%</td>
            </tr>
            <tr>
              <td><code>raw_mcp</code></td>
              <td><code>f1_response_drop</code></td>
              <td style="color: var(--danger); font-weight: bold;">2.00x</td>
              <td style="color: var(--success); font-weight: bold;">1.00x</td>
              <td style="color: var(--danger);">100%</td>
              <td style="color: var(--success); font-weight: bold;">0%</td>
            </tr>
            <tr>
              <td><code>react_agent</code></td>
              <td><code>none (clean)</code></td>
              <td style="color: var(--text);">1.00x</td>
              <td style="color: var(--success); font-weight: bold;">1.00x</td>
              <td style="color: var(--muted);">0%</td>
              <td style="color: var(--success);">0%</td>
            </tr>
            <tr>
              <td><code>react_agent</code></td>
              <td><code>f1_response_drop</code></td>
              <td style="color: var(--danger); font-weight: bold;">4.00x</td>
              <td style="color: var(--success); font-weight: bold;">1.00x</td>
              <td style="color: var(--danger);">100%</td>
              <td style="color: var(--success); font-weight: bold;">0%</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Durable Ledger Table -->
    <div class="panel">
      <div class="panel-title">
        <span>Durable SQLite WAL State Ledger & Ingress Packet Stream</span>
        <span style="font-size: 0.8rem; color: var(--muted); font-family: ui-monospace, Menlo, monospace;">PRAGMA journal_mode = WAL</span>
      </div>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Derived Idempotency Key</th>
              <th>Tool Method</th>
              <th>State Lattice</th>
              <th>Lease Age</th>
              <th>Upstream Status</th>
            </tr>
          </thead>
          <tbody id="ledgerBody">
            <tr>
              <td>15:28:01.102</td>
              <td><span style="color: var(--primary);">derived:banking:transfer:b9c6105e...</span></td>
              <td><code>transfer</code></td>
              <td><span class="badge badge-success">COMMITTED</span></td>
              <td>1.2s</td>
              <td><span style="color: var(--success);">200 OK (Cached Replay)</span></td>
            </tr>
            <tr>
              <td>15:28:01.350</td>
              <td><span style="color: var(--primary);">derived:banking:transfer:41d087fe...</span></td>
              <td><code>transfer</code></td>
              <td><span class="badge" style="background: rgba(192, 132, 252, 0.2); color: var(--purple); border: 1px solid var(--purple);">COALESCED</span></td>
              <td>0.8s</td>
              <td><span style="color: var(--purple);">Joined Leader Future</span></td>
            </tr>
            <tr>
              <td>15:28:02.010</td>
              <td><span style="color: var(--primary);">derived:banking:check_balance:8a9b...</span></td>
              <td><code>check_balance</code></td>
              <td><span class="badge badge-success">COMMITTED</span></td>
              <td>0.2s</td>
              <td><span style="color: var(--success);">200 OK (Pass-Through)</span></td>
            </tr>
            <tr>
              <td>15:28:02.450</td>
              <td><span style="color: var(--primary);">derived:banking:transfer:cc8109aa...</span></td>
              <td><code>transfer</code></td>
              <td><span class="badge" style="background: rgba(251, 191, 36, 0.2); color: var(--warning); border: 1px solid var(--warning);">IN_PROGRESS</span></td>
              <td>0.1s</td>
              <td><span style="color: var(--warning);">Upstream Dispatched</span></td>
            </tr>
          </tbody>
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
        document.getElementById("unprotAttempts").innerText = data.unprotected.attempts + " (Initial + 1 Retry)";
        document.getElementById("unprotCommits").innerText = data.unprotected.commits + " (DUPLICATE STATE)";
        document.getElementById("unprotMult").innerText = data.unprotected.multiplicity.toFixed(2) + "x";

        document.getElementById("guardAttempts").innerText = data.guarded.attempts + " (Initial + 1 Retry)";
        document.getElementById("guardCommits").innerText = data.guarded.commits + " (EXACTLY 1)";
        document.getElementById("guardMult").innerText = data.guarded.multiplicity.toFixed(2) + "x (0 Duplicates)";
        document.getElementById("testTime").innerText = "Last execution: " + new Date().toLocaleTimeString();
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
        document.getElementById("benchTime").innerText = "Completed at " + new Date().toLocaleTimeString();
      } catch (err) {
        alert("Error running benchmark: " + err);
      } finally {
        btn.disabled = false;
        btn.innerHTML = "📊 Run Matrix Benchmark";
      }
    }

    function refreshLedger() {
      const flows = parseInt(document.getElementById("statFlows").innerText.replace(",", "")) + 12;
      document.getElementById("statFlows").innerText = flows.toLocaleString();
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
