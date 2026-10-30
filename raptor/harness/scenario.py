"""
YAML-driven scenario definition and deterministic execution engine.
Implements Requirement H1:
'Run a scenario (stack, workload, fault, seed, repetitions) from one YAML file.
Acceptance criterion: Same file and seed reproduce an identical fault-event log.'
"""
from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from typing import Any, Dict, List, Optional
import uvicorn
import yaml
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
from raptor.harness.toxiproxy import ToxiproxyController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server

logger = logging.getLogger(__name__)


class StackConfig(BaseModel):
    type: str = "raw_mcp"  # "raw_mcp" | "react_agent"
    max_retries: int = 1
    backoff_seconds: float = 0.05
    timeout_seconds: float = 5.0


class WorkloadConfig(BaseModel):
    type: str = "transfer"
    amount: float = 50.0
    recipient: str = "TargetAccount"
    memo_prefix: str = "scenario_tx"


class FaultConfig(BaseModel):
    type: str = "none"  # "none" | "f1_response_drop" | "f3_latency" | "f4_timeout" | "conn_reset"
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ScenarioConfig(BaseModel):
    name: str
    version: str = "1.0"
    seed: int = 42
    repetitions: int = 5
    use_guard: bool = False
    stack: StackConfig = Field(default_factory=StackConfig)
    workload: WorkloadConfig = Field(default_factory=WorkloadConfig)
    fault: FaultConfig = Field(default_factory=FaultConfig)

    @classmethod
    def from_yaml_file(cls, path: str) -> "ScenarioConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


class FaultEvent(BaseModel):
    trial_index: int
    event_type: str
    op_id: str
    client_success: bool
    attempts_made: int
    committed_count: int
    has_duplicate: bool
    is_phantom: bool


class ScenarioResult(BaseModel):
    scenario_name: str
    seed: int
    repetitions: int
    fault_events: List[FaultEvent]
    multiplicity: float
    duplicate_count: int
    phantom_error_count: int
    success_rate: float
    details: List[EvaluationResult]
    report: Optional[AnalysisReport] = None


class ScenarioRunner:
    """Executes a ScenarioConfig deterministically."""

    def __init__(
        self,
        config: ScenarioConfig,
        server_port: int = 8971,
        proxy_port: int = 8972,
        toxiproxy_api: str = "http://127.0.0.1:8474",
    ):
        self.config = config
        self.server_port = server_port
        self.proxy_port = proxy_port
        self.toxiproxy = ToxiproxyController(toxiproxy_api)
        self.proxy_name = f"scenario_proxy_{config.seed}"
        self.use_native_proxy = not self.toxiproxy.is_reachable()
        self.asgi_fault_proxy: Optional[ASGIFaultProxy] = None
        self.asgi_fault_srv: Optional[uvicorn.Server] = None
        self.asgi_fault_task: Optional[asyncio.Task] = None

    def _create_client(self, client_url: str):
        stype = self.config.stack.type.lower()
        if stype in ("react_agent", "react"):
            return ReActAgentAdapter(
                endpoint_url=client_url,
                max_agent_steps=self.config.stack.max_retries + 1,
                client_retries=1,
            )
        elif stype in ("langgraph", "langgraph_agent"):
            return LangGraphAgentAdapter(
                endpoint_url=client_url,
                max_graph_iterations=self.config.stack.max_retries + 1,
                sdk_retries=0,
            )
        elif stype in ("openai", "openai_agent", "openai_agents_sdk"):
            return OpenAIAgentsSDKAdapter(
                endpoint_url=client_url,
                max_agent_turns=self.config.stack.max_retries + 1,
            )
        elif stype in ("a2a", "a2a_adapter"):
            return A2AClientAdapter(
                endpoint_url=client_url,
                max_retries=self.config.stack.max_retries,
            )
        elif stype in ("model_agent", "w2"):
            return ModelAgentAdapter(
                endpoint_url=client_url,
                max_re_plans=self.config.stack.max_retries,
            )
        elif stype in ("durable", "b4", "durable_execution"):
            return DurableExecutionAdapter(
                endpoint_url=client_url,
                max_activity_retries=self.config.stack.max_retries,
                workflow_id=f"wf_{self.config.seed}",
            )
        else:
            return RawMCPClientAdapter(
                endpoint_url=client_url,
                max_retries=self.config.stack.max_retries,
                backoff_seconds=self.config.stack.backoff_seconds,
                timeout_seconds=self.config.stack.timeout_seconds,
            )

    async def run(self) -> ScenarioResult:
        # Seed the deterministic pseudo-random generator
        random.seed(self.config.seed)

        ledger = LedgerDB()
        server, _ = create_mock_mcp_server(ledger=ledger)
        app = server.streamable_http_app()

        uvicorn_cfg = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.server_port,
            log_level="error",
        )
        srv = uvicorn.Server(uvicorn_cfg)
        server_task = asyncio.create_task(srv.serve())
        await asyncio.sleep(0.3)

        oracle = Oracle(ledger=ledger)
        fault_events: List[FaultEvent] = []
        evaluations: List[EvaluationResult] = []

        try:
            if self.use_native_proxy:
                self.asgi_fault_proxy = ASGIFaultProxy(
                    upstream_url=f"http://127.0.0.1:{self.server_port}"
                )
                fault_cfg = uvicorn.Config(
                    self.asgi_fault_proxy.as_asgi_app(),
                    host="127.0.0.1",
                    port=self.proxy_port,
                    log_level="error",
                )
                self.asgi_fault_srv = uvicorn.Server(fault_cfg)
                self.asgi_fault_task = asyncio.create_task(self.asgi_fault_srv.serve())
                await asyncio.sleep(0.3)
            else:
                self.toxiproxy.delete_proxy(self.proxy_name)
                self.toxiproxy.create_proxy(
                    name=self.proxy_name,
                    listen=f"127.0.0.1:{self.proxy_port}",
                    upstream=f"127.0.0.1:{self.server_port}",
                )

            proxy_url = f"http://127.0.0.1:{self.proxy_port}/mcp"

            # Optional Guard Proxy deployment (Experiment E3)
            guard_task = None
            guard_srv = None
            guard_app = None
            if self.config.use_guard:
                guard_policy = GuardPolicyConfig.from_yaml_file("raptor/policies/guard_policy.yaml")
                guard_ledger = GuardLedger()
                guard_app = GuardProxyApp(
                    upstream_url=f"http://127.0.0.1:{self.proxy_port}",
                    policy_config=guard_policy,
                    ledger=guard_ledger,
                )
                guard_port = self.server_port + 2
                guard_cfg = uvicorn.Config(
                    guard_app.as_asgi_app(),
                    host="127.0.0.1",
                    port=guard_port,
                    log_level="error",
                )
                guard_srv = uvicorn.Server(guard_cfg)
                guard_task = asyncio.create_task(guard_srv.serve())
                await asyncio.sleep(0.3)
                client_url = f"http://127.0.0.1:{guard_port}/mcp"
            else:
                client_url = proxy_url

            # Execute repetitions
            for i in range(self.config.repetitions):
                # Deterministic memo derivation
                memo = f"{self.config.workload.memo_prefix}_{self.config.seed}_{i}"
                
                client = self._create_client(client_url)

                # Initialize handshake while connection is clean
                if hasattr(client, "_ensure_initialized"):
                    await client._ensure_initialized()

                # Apply fault
                self._apply_fault()

                # Execute workload
                client_res = await client.execute_transfer(
                    amount=self.config.workload.amount,
                    recipient=self.config.workload.recipient,
                    memo=memo,
                )

                # Small settle delay for SQLite WAL sync
                await asyncio.sleep(0.05)

                # Evaluate with Oracle
                eval_res = oracle.evaluate_task(
                    logical_op_id=memo,
                    client_reported_success=client_res.success,
                    client_error=client_res.error_message,
                    total_attempts=client_res.attempts_made,
                )
                evaluations.append(eval_res)

                event = FaultEvent(
                    trial_index=i,
                    event_type=self.config.fault.type,
                    op_id=memo,
                    client_success=client_res.success,
                    attempts_made=client_res.attempts_made,
                    committed_count=eval_res.committed_effects_count,
                    has_duplicate=eval_res.has_duplicate,
                    is_phantom=eval_res.is_phantom_failure,
                )
                fault_events.append(event)

                # Clear fault before next iteration
                self._clear_fault()
                if hasattr(client, "close"):
                    await client.close()

            total_committed = sum(e.committed_effects_count for e in evaluations)
            multiplicity = total_committed / float(self.config.repetitions) if self.config.repetitions else 0.0
            duplicates = sum(1 for e in evaluations if e.has_duplicate)
            phantoms = sum(1 for e in evaluations if e.is_phantom_failure)
            success_count = sum(1 for e in evaluations if e.client_reported_success)
            success_rate = success_count / float(self.config.repetitions) if self.config.repetitions else 0.0

            analyzer = StatisticalAnalyzer(seed=self.config.seed)
            report = analyzer.analyze(
                evaluations,
                plan_meta={"scenario": self.config.name, "seed": self.config.seed, "repetitions": self.config.repetitions},
            )

            return ScenarioResult(
                scenario_name=self.config.name,
                seed=self.config.seed,
                repetitions=self.config.repetitions,
                fault_events=fault_events,
                multiplicity=multiplicity,
                duplicate_count=duplicates,
                phantom_error_count=phantoms,
                success_rate=success_rate,
                details=evaluations,
                report=report,
            )

        finally:
            if guard_app:
                await guard_app.close()
            if guard_srv:
                guard_srv.should_exit = True
            if guard_task:
                await guard_task
            if self.asgi_fault_proxy:
                await self.asgi_fault_proxy.close()
            if self.asgi_fault_srv:
                self.asgi_fault_srv.should_exit = True
            if self.asgi_fault_task:
                await self.asgi_fault_task
            if not self.use_native_proxy:
                self.toxiproxy.delete_proxy(self.proxy_name)
            srv.should_exit = True
            await server_task

    def _apply_fault(self) -> None:
        ftype = self.config.fault.type
        params = self.config.fault.parameters
        if self.use_native_proxy:
            if self.asgi_fault_proxy:
                self.asgi_fault_proxy.set_fault(ftype, params)
            return

        if ftype == "f1_response_drop":
            timeout_ms = params.get("timeout_ms", 10)
            self.toxiproxy.setup_f1_response_drop(self.proxy_name, timeout_ms=timeout_ms)
        elif ftype == "f3_latency":
            lat = params.get("latency_ms", 100)
            jit = params.get("jitter_ms", 20)
            self.toxiproxy.setup_f3_latency_jitter(self.proxy_name, latency_ms=lat, jitter_ms=jit)
        elif ftype == "f4_timeout":
            t_ms = params.get("timeout_ms", 5000)
            self.toxiproxy.setup_f4_timeout(self.proxy_name, timeout_ms=t_ms)
        elif ftype == "conn_reset":
            stream = params.get("stream", "upstream")
            self.toxiproxy.setup_connection_reset(self.proxy_name, stream=stream)

    def _clear_fault(self) -> None:
        if self.use_native_proxy:
            if self.asgi_fault_proxy:
                self.asgi_fault_proxy.clear_fault()
            return

        self.toxiproxy.clear_f1_response_drop(self.proxy_name)
        self.toxiproxy.clear_f3_latency_jitter(self.proxy_name)
        self.toxiproxy.clear_f4_timeout(self.proxy_name)
        self.toxiproxy.clear_connection_reset(self.proxy_name)


async def _main() -> None:
    """CLI execution for ScenarioRunner (Requirement A3)."""
    if len(sys.argv) < 2:
        print("Usage: python3 -m raptor.harness.scenario <scenario.yaml>")
        sys.exit(1)

    scenario_file = sys.argv[1]
    cfg = ScenarioConfig.from_yaml_file(scenario_file)
    runner = ScenarioRunner(cfg)
    result = await runner.run()

    if result.report:
        print(result.report.to_ascii_table())
    else:
        print(
            f"Scenario: {result.scenario_name} | "
            f"Multiplicity: {result.multiplicity:.2f} | "
            f"Duplicates: {result.duplicate_count}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
