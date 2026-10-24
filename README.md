# RAPTOR (PartitionGuard)

Fault-injection harness and protocol-aware sidecar proxy to protect against dropped or retried agent tool calls duplicating side effects or amplifying backend load.

## Gate G0 Results: PASSED

Under Fault $F_1$ (response dropped on wire after server-side commit):
- **Baseline ($B_0$):** Multiplicity = 1.00, 0 duplicates, 100% success.
- **MCP SDK Retry Layer:** Multiplicity = 2.00, 100% duplicate rate, 100% phantom failure rate.
- **ReAct Agent Loop + SDK Retries:** Multiplicity = 4.00 (compounded across framework re-plans and SDK retries).
- **Hypothesis $H_1$ Status:** **CONFIRMED**. Real agent stacks duplicate side effects and multiply backend load under network-level response drop.

## Guard Proxy Verification (Hypothesis $H_3$): PASSED

Under Fault $F_1$ with RAPTOR Guard Proxy deployed:
- **Backend Multiplicity:** **1.00** (reduced from 2.00 / 4.00 without Guard).
- **Backend Duplicates:** **0% (0 duplicates)** (reduced from 100% duplicate rate without Guard).
- **Coalescing:** 2 to 20 concurrent parallel calls join onto **exactly 1** upstream backend commit.
- **Durable Ledger:** SQLite WAL with crash-recovery transitioning mid-flight requests to `UNKNOWN`.

---

## Directory Structure

```
raptor/
├── pyproject.toml                     # Dependencies (mcp, starlette, fastapi, uvicorn, pyyaml, pytest)
├── scenarios/                         # Versioned YAML scenario definitions
│   ├── baseline.yaml                  # Baseline run without faults
│   ├── f1_response_drop.yaml          # F1 response drop with retries
│   ├── f3_latency.yaml                # F3 added latency and jitter
│   └── f4_timeout.yaml                # F4 connection timeout
├── raptor/
│   ├── common/
│   │   └── models.py                  # ExecutionRecord, AttemptRecord, ToolPolicy, LedgerState
│   ├── guard/                         # RAPTOR Protocol-Aware Guard Proxy
│   │   ├── ledger.py                  # Durable SQLite WAL ledger & crash recovery
│   │   ├── coalescer.py               # In-flight RequestCoalescer (Singleflight)
│   │   ├── policy.py                  # Tool policy loader and validator
│   │   ├── unknown_outcome.py         # Standardized UNKNOWN_OUTCOME MCP payload builder
│   │   └── proxy.py                   # Starlette ASGI reverse proxy for MCP Streamable HTTP
│   ├── harness/
│   │   ├── toxiproxy.py               # Toxiproxy controller (F1, F3, F4, Reset, F7)
│   │   ├── oracle.py                  # SQLite ground-truth effect evaluator
│   │   ├── scenario.py                # YAML scenario runner with deterministic seed
│   │   └── runner_g0.py               # Gate G0 experiment runner and CLI
│   ├── server/
│   │   ├── ledger_db.py               # Append-only SQLite WAL ledger
│   │   └── mock_tool_server.py        # MCP Streamable HTTP mock banking server
│   ├── clients/
│   │   ├── base.py                    # Client adapter interface
│   │   ├── raw_mcp_client.py          # MCP Streamable HTTP SDK adapter
│   │   ├── react_agent.py             # ReAct agent loop adapter
│   │   ├── langgraph_adapter.py       # LangGraph StateGraph workflow adapter
│   │   ├── openai_agent_adapter.py    # OpenAI Agents SDK function-calling adapter
│   │   └── behavior_probe.py          # Probe for isError: true handling
│   └── policies/
│       ├── transfer_policy.py         # Key derivation with masking and time-windowing
│       └── guard_policy.yaml          # Default tool policies
└── tests/
    ├── test_mcp_protocol.py           # MCP 2.x 2026-07-28 protocol & request ID checks
    ├── test_behavior_probe.py         # Probe verifying client handling of isError: true
    ├── test_toxiproxy_f1.py           # Verification of Fault F1 (response lost after commit)
    ├── test_toxiproxy_fault_grid.py   # Fault grid: F3 latency, F4 timeout, TCP reset
    ├── test_smoke_g0.py               # End-to-end smoke test of Gate G0 harness
    ├── test_oracle_validation_50.py   # 50 hand-labeled runs validation of Oracle
    ├── test_scenario_reproducibility.py # YAML scenario deterministic replay test
    ├── test_stack_adapters_no_fault.py # 100% no-fault smoke test across 4 adapters
    ├── test_guard_proxy.py            # Guard proxy dedup, coalescing, crash recovery
    └── test_guard_under_f1.py         # Protection test: Guard eliminates duplicates under F1
```

---

## Running the Test Suite & Scenarios

### Prerequisites
1. Python 3.11+
2. Toxiproxy (`brew install toxiproxy` and `toxiproxy-server` running on `localhost:8474`)

### Run All Tests
```bash
.venv/bin/pytest tests/ -v
```

### Run Gate G0 Matrix
```bash
.venv/bin/python3 -m raptor.harness.runner_g0
```
