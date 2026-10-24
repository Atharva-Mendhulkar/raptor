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
import time
from typing import Any, Dict, List, Optional
import uvicorn
import yaml
from pydantic import BaseModel, Field

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.clients.react_agent import ReActAgentAdapter
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
            self.toxiproxy.delete_proxy(self.proxy_name)
            self.toxiproxy.create_proxy(
                name=self.proxy_name,
                listen=f"127.0.0.1:{self.proxy_port}",
                upstream=f"127.0.0.1:{self.server_port}",
            )

            proxy_url = f"http://127.0.0.1:{self.proxy_port}/mcp"

            # Execute repetitions
            for i in range(self.config.repetitions):
                # Deterministic memo derivation
                memo = f"{self.config.workload.memo_prefix}_{self.config.seed}_{i}"
                
                # Client initialization
                if self.config.stack.type == "react_agent":
                    client = ReActAgentAdapter(
                        endpoint_url=proxy_url,
                        max_agent_steps=self.config.stack.max_retries + 1,
                        client_retries=1,
                    )
                else:
                    client = RawMCPClientAdapter(
                        endpoint_url=proxy_url,
                        max_retries=self.config.stack.max_retries,
                        backoff_seconds=self.config.stack.backoff_seconds,
                        timeout_seconds=self.config.stack.timeout_seconds,
                    )

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

            total_committed = sum(e.committed_effects_count for e in evaluations)
            multiplicity = total_committed / float(self.config.repetitions) if self.config.repetitions else 0.0
            duplicates = sum(1 for e in evaluations if e.has_duplicate)
            phantoms = sum(1 for e in evaluations if e.is_phantom_failure)
            success_count = sum(1 for e in evaluations if e.client_reported_success)
            success_rate = success_count / float(self.config.repetitions) if self.config.repetitions else 0.0

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
            )

        finally:
            self.toxiproxy.delete_proxy(self.proxy_name)
            srv.should_exit = True
            await server_task

    def _apply_fault(self) -> None:
        ftype = self.config.fault.type
        params = self.config.fault.parameters
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
        self.toxiproxy.clear_f1_response_drop(self.proxy_name)
        self.toxiproxy.clear_f3_latency_jitter(self.proxy_name)
        self.toxiproxy.clear_f4_timeout(self.proxy_name)
        self.toxiproxy.clear_connection_reset(self.proxy_name)
