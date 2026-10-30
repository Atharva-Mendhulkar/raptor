"""
Full Benchmark Matrix Runner.
Runs end-to-end combinations across:
- 7 Client Stack Adapters (Raw MCP, ReAct, LangGraph, OpenAI, A2A, Model Agent W2, Durable B4)
- 4 Fault Conditions (F0 Clean, F1 Response Drop, F3 Latency, F4 Timeout)
- 2 Protection Modes (Unprotected vs Guarded)
Generates publication-ready Markdown and ASCII tables with Bootstrap 95% CIs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional
import uvicorn
from pydantic import BaseModel, Field

from raptor.analyzer.metrics import AnalysisReport, StatisticalAnalyzer
from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.clients.react_agent import ReActAgentAdapter
from raptor.clients.langgraph_adapter import LangGraphAgentAdapter
from raptor.clients.openai_agent_adapter import OpenAIAgentsSDKAdapter
from raptor.clients.a2a_adapter import A2AClientAdapter
from raptor.clients.model_agent import ModelAgentAdapter
from raptor.clients.durable_adapter import DurableExecutionAdapter
from raptor.guard.ledger import GuardLedger
from raptor.guard.policy import GuardPolicyConfig
from raptor.guard.proxy import GuardProxyApp
from raptor.harness.asgi_fault_proxy import ASGIFaultProxy
from raptor.harness.oracle import EvaluationResult, Oracle
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server

logger = logging.getLogger(__name__)


class MatrixCellResult(BaseModel):
    adapter_name: str
    fault_name: str
    guarded: bool
    trials: int
    success_rate: float
    duplicate_rate: float
    phantom_rate: float
    multiplicity: float
    multiplicity_ci_low: float
    multiplicity_ci_high: float
    upstream_forwards: int = 0
    dedup_replays: int = 0


class MatrixBenchmarkReport(BaseModel):
    total_cells: int
    trials_per_cell: int
    cells: List[MatrixCellResult] = Field(default_factory=list)

    def to_markdown_table(self) -> str:
        """Render a comparative markdown table comparing Unprotected vs Guarded."""
        lines = [
            "| Stack Adapter | Fault Mode | Unprotected Mult (CI) | Guarded Mult (CI) | Unprotected Dup% | Guarded Dup% |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        # Group by (adapter, fault)
        grouped: Dict[tuple, Dict[str, MatrixCellResult]] = {}
        for c in self.cells:
            key = (c.adapter_name, c.fault_name)
            if key not in grouped:
                grouped[key] = {}
            mode = "guarded" if c.guarded else "unprotected"
            grouped[key][mode] = c

        for (adapter, fault), pair in grouped.items():
            unprot = pair.get("unprotected")
            guard = pair.get("guarded")

            unprot_m = f"{unprot.multiplicity:.2f} [{unprot.multiplicity_ci_low:.2f}-{unprot.multiplicity_ci_high:.2f}]" if unprot else "N/A"
            guard_m = f"{guard.multiplicity:.2f} [{guard.multiplicity_ci_low:.2f}-{guard.multiplicity_ci_high:.2f}]" if guard else "N/A"
            unprot_d = f"{unprot.duplicate_rate * 100:.1f}%" if unprot else "N/A"
            guard_d = f"{guard.duplicate_rate * 100:.1f}%" if guard else "N/A"

            lines.append(f"| {adapter} | {fault} | {unprot_m} | {guard_m} | {unprot_d} | {guard_d} |")

        return "\n".join(lines)


class MatrixRunner:
    """Executes a parameterized grid of Stack Adapters x Faults x Guarded."""

    def __init__(
        self,
        server_port: int = 8981,
        fault_port: int = 8982,
        guard_port: int = 8983,
        seed: int = 42,
    ):
        self.server_port = server_port
        self.fault_port = fault_port
        self.guard_port = guard_port
        self.seed = seed

    def _create_adapter(self, adapter_name: str, endpoint_url: str):
        if adapter_name == "raw_mcp":
            return RawMCPClientAdapter(endpoint_url, max_retries=1)
        elif adapter_name == "react_agent":
            return ReActAgentAdapter(endpoint_url, max_agent_replans=1, sdk_retries=1)
        elif adapter_name == "langgraph":
            return LangGraphAgentAdapter(endpoint_url, max_graph_iterations=1, sdk_retries=0)
        elif adapter_name == "openai":
            return OpenAIAgentsSDKAdapter(endpoint_url, max_agent_turns=1, sdk_retries=0)
        elif adapter_name == "a2a":
            return A2AClientAdapter(endpoint_url, max_retries=1)
        elif adapter_name == "model_agent":
            return ModelAgentAdapter(endpoint_url, max_re_plans=1)
        elif adapter_name == "durable":
            return DurableExecutionAdapter(endpoint_url, max_activity_retries=1, workflow_id=f"wf_mat_{self.seed}")
        else:
            raise ValueError(f"Unknown adapter: {adapter_name}")

    async def run_cell(
        self,
        adapter_name: str,
        fault_type: str,
        guarded: bool,
        trials: int = 5,
    ) -> MatrixCellResult:
        """Run single matrix cell evaluation."""
        backend_ledger = LedgerDB()
        server, _ = create_mock_mcp_server(ledger=backend_ledger)
        server_app = server.streamable_http_app()

        srv = uvicorn.Server(uvicorn.Config(server_app, host="127.0.0.1", port=self.server_port, log_level="error"))
        srv_task = asyncio.create_task(srv.serve())

        guard_app = None
        guard_srv = None
        guard_task = None
        guard_ledger = None

        fault_proxy = None
        fault_srv = None
        fault_task = None

        await asyncio.sleep(0.25)
        try:
            if guarded:
                # Topology: Client -> Fault Proxy -> Guard Proxy -> Mock Server
                guard_policy = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
                guard_ledger = GuardLedger()
                guard_app = GuardProxyApp(
                    upstream_url=f"http://127.0.0.1:{self.server_port}",
                    policy_config=guard_policy,
                    ledger=guard_ledger,
                )
                guard_srv = uvicorn.Server(uvicorn.Config(guard_app.as_asgi_app(), host="127.0.0.1", port=self.guard_port, log_level="error"))
                guard_task = asyncio.create_task(guard_srv.serve())

                fault_proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{self.guard_port}")
                fault_srv = uvicorn.Server(uvicorn.Config(fault_proxy.as_asgi_app(), host="127.0.0.1", port=self.fault_port, log_level="error"))
                fault_task = asyncio.create_task(fault_srv.serve())
            else:
                # Topology: Client -> Fault Proxy -> Mock Server
                fault_proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{self.server_port}")
                fault_srv = uvicorn.Server(uvicorn.Config(fault_proxy.as_asgi_app(), host="127.0.0.1", port=self.fault_port, log_level="error"))
                fault_task = asyncio.create_task(fault_srv.serve())

            await asyncio.sleep(0.3)
            client_url = f"http://127.0.0.1:{self.fault_port}/mcp"

            oracle = Oracle(ledger=backend_ledger)
            evaluations: List[EvaluationResult] = []

            for i in range(trials):
                memo = f"mat_{adapter_name}_{fault_type}_{'g' if guarded else 'u'}_{i}"
                client = self._create_adapter(adapter_name, client_url)

                # Initialize while fault is inactive
                fault_proxy.clear_fault()
                if hasattr(client, "_ensure_initialized"):
                    await client._ensure_initialized()

                # Activate fault
                if fault_type == "f1_response_drop":
                    fault_proxy.set_fault("f1_response_drop")
                elif fault_type == "f3_latency":
                    fault_proxy.set_fault("f3_latency", {"latency_ms": 100, "jitter_ms": 20})
                elif fault_type == "f4_timeout":
                    fault_proxy.set_fault("f4_timeout", {"timeout_ms": 3000})

                idempotency_key = f"key_{memo}" if (adapter_name == "a2a" or guarded) else None
                res = await client.execute_transfer(amount=50.0, recipient="MatTarget", memo=memo, idempotency_key=idempotency_key)
                await asyncio.sleep(0.05)

                eval_res = oracle.evaluate_task(
                    logical_op_id=memo,
                    client_reported_success=res.success,
                    client_error=res.error_message,
                    total_attempts=res.attempts_made,
                )
                evaluations.append(eval_res)
                await client.close()

            analyzer = StatisticalAnalyzer(seed=self.seed)
            report = analyzer.analyze(evaluations, plan_meta={"adapter": adapter_name, "fault": fault_type})

            up_forwards = guard_app.stats["upstream_forwards"] if guard_app else 0
            dedup_replays = guard_app.stats["dedup_replays"] if guard_app else 0

            return MatrixCellResult(
                adapter_name=adapter_name,
                fault_name=fault_type,
                guarded=guarded,
                trials=trials,
                success_rate=report.task_success_rate.mean,
                duplicate_rate=report.duplicate_rate.mean,
                phantom_rate=report.phantom_failure_rate.mean,
                multiplicity=report.multiplicity.mean,
                multiplicity_ci_low=report.multiplicity.ci_low,
                multiplicity_ci_high=report.multiplicity.ci_high,
                upstream_forwards=up_forwards,
                dedup_replays=dedup_replays,
            )

        finally:
            if fault_proxy:
                await fault_proxy.close()
            if fault_srv:
                fault_srv.should_exit = True
            if fault_task:
                await fault_task
            if guard_app:
                await guard_app.close()
            if guard_srv:
                guard_srv.should_exit = True
            if guard_task:
                await guard_task
            srv.should_exit = True
            await srv_task

    async def run_matrix(
        self,
        adapters: Optional[List[str]] = None,
        faults: Optional[List[str]] = None,
        modes: Optional[List[bool]] = None,
        trials_per_cell: int = 5,
    ) -> MatrixBenchmarkReport:
        target_adapters = adapters or ["raw_mcp", "react_agent", "langgraph", "openai", "a2a", "model_agent", "durable"]
        target_faults = faults or ["none", "f1_response_drop", "f3_latency", "f4_timeout"]
        target_modes = modes if modes is not None else [False, True]

        report = MatrixBenchmarkReport(
            total_cells=len(target_adapters) * len(target_faults) * len(target_modes),
            trials_per_cell=trials_per_cell,
        )

        for adapter in target_adapters:
            for fault in target_faults:
                for guarded in target_modes:
                    logger.info(f"Running cell: adapter={adapter}, fault={fault}, guarded={guarded}")
                    cell = await self.run_cell(adapter, fault, guarded, trials=trials_per_cell)
                    report.cells.append(cell)

        return report
