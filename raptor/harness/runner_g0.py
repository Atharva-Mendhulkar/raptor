"""Gate G0 Experiment Runner: Empirical verification of Hypothesis H1 under Fault F1."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List
import uvicorn
from pydantic import BaseModel

from raptor.clients.raw_mcp_client import RawMCPClientAdapter
from raptor.clients.react_agent import ReActAgentAdapter
from raptor.clients.behavior_probe import ToolErrorBehaviorProbe
from raptor.harness.asgi_fault_proxy import ASGIFaultProxy
from raptor.harness.oracle import EvaluationResult, Oracle
from raptor.harness.toxiproxy import ToxiproxyController
from raptor.server.ledger_db import LedgerDB
from raptor.server.mock_tool_server import create_mock_mcp_server

logger = logging.getLogger(__name__)


class G0ExperimentSummary(BaseModel):
    trials_per_condition: int
    baseline_success_rate: float
    baseline_multiplicity: float
    baseline_duplicates: int
    f1_sdk_retry_multiplicity: float
    f1_sdk_retry_duplicates: int
    f1_sdk_phantom_failures: int
    f1_react_multiplicity: float
    f1_react_duplicates: int
    f1_react_phantom_failures: int
    h1_confirmed: bool
    verdict: str
    details: List[EvaluationResult] = []


class G0Runner:
    """Orchestrates the Gate G0 experiment."""

    def __init__(
        self,
        server_port: int = 8991,
        proxy_port: int = 8992,
        toxiproxy_api: str = "http://127.0.0.1:8474",
    ):
        self.server_port = server_port
        self.proxy_port = proxy_port
        self.toxiproxy_api = toxiproxy_api
        self.toxiproxy = ToxiproxyController(self.toxiproxy_api)
        self.proxy_name = "raptor_g0_proxy"
        self.use_native_proxy = not self.toxiproxy.is_reachable()

    async def run_experiment(self, num_trials: int = 5) -> G0ExperimentSummary:
        """Run the complete Gate G0 test suite."""
        logger.info("Initializing Gate G0 test harness...")
        
        # 1. Initialize SQLite Ledger and MCPServer
        ledger = LedgerDB()
        server, _ = create_mock_mcp_server(ledger=ledger)
        starlette_app = server.streamable_http_app()

        # 2. Launch Mock Server in background
        config = uvicorn.Config(
            starlette_app,
            host="127.0.0.1",
            port=self.server_port,
            log_level="error",
        )
        uvicorn_server = uvicorn.Server(config)
        server_task = asyncio.create_task(uvicorn_server.serve())
        await asyncio.sleep(0.5)

        oracle = Oracle(ledger)
        details: List[EvaluationResult] = []
        fault_proxy = None
        fault_srv = None
        fault_task = None

        try:
            if self.use_native_proxy:
                fault_proxy = ASGIFaultProxy(upstream_url=f"http://127.0.0.1:{self.server_port}")
                fault_cfg = uvicorn.Config(
                    fault_proxy.as_asgi_app(),
                    host="127.0.0.1",
                    port=self.proxy_port,
                    log_level="error",
                )
                fault_srv = uvicorn.Server(fault_cfg)
                fault_task = asyncio.create_task(fault_srv.serve())
                await asyncio.sleep(0.3)
            else:
                self.toxiproxy.create_proxy(
                    name=self.proxy_name,
                    listen=f"127.0.0.1:{self.proxy_port}",
                    upstream=f"127.0.0.1:{self.server_port}",
                )

            proxy_endpoint = f"http://127.0.0.1:{self.proxy_port}/mcp"

            # -------------------------------------------------------------
            # Phase A: Baseline B0 (No Faults)
            # -------------------------------------------------------------
            logger.info("--- Running Phase A: Baseline (No Faults) ---")
            baseline_results = []
            raw_client = RawMCPClientAdapter(proxy_endpoint, max_retries=0)

            for i in range(num_trials):
                op_id = f"baseline_tx_{uuid.uuid4().hex[:6]}"
                res = await raw_client.execute_transfer(amount=50.0, recipient="Bob", memo=op_id)
                eval_res = oracle.evaluate_task(op_id, res.success, res.error_message, res.attempts_made)
                baseline_results.append(eval_res)
                details.append(eval_res)
            await raw_client.close()

            # -------------------------------------------------------------
            # Phase B: Fault F1 (Response dropped after commit) with SDK retry
            # -------------------------------------------------------------
            logger.info("--- Running Phase B: Fault F1 with SDK retry (max_retries=1) ---")
            f1_sdk_results = []

            for i in range(num_trials):
                if self.use_native_proxy:
                    fault_proxy.clear_fault()
                else:
                    self.toxiproxy.clear_f1_response_drop(self.proxy_name)

                sdk_retry_client = RawMCPClientAdapter(proxy_endpoint, max_retries=1, backoff_seconds=0.05)
                await sdk_retry_client._ensure_initialized()

                if self.use_native_proxy:
                    fault_proxy.set_fault("f1_response_drop")
                else:
                    self.toxiproxy.setup_f1_response_drop(self.proxy_name, timeout_ms=10)

                op_id = f"f1_sdk_tx_{uuid.uuid4().hex[:6]}"
                res = await sdk_retry_client.execute_transfer(amount=100.0, recipient="Alice", memo=op_id)
                await asyncio.sleep(0.05)
                eval_res = oracle.evaluate_task(op_id, res.success, res.error_message, res.attempts_made)
                f1_sdk_results.append(eval_res)
                details.append(eval_res)
                await sdk_retry_client.close()

            # -------------------------------------------------------------
            # Phase C: Fault F1 with ReAct Agent Loop (Framework Re-plans)
            # -------------------------------------------------------------
            logger.info("--- Running Phase C: Fault F1 with ReAct Loop (Re-plans=1, SDK retries=1) ---")
            f1_react_results = []

            for i in range(num_trials):
                if self.use_native_proxy:
                    fault_proxy.clear_fault()
                else:
                    self.toxiproxy.clear_f1_response_drop(self.proxy_name)

                react_client = ReActAgentAdapter(proxy_endpoint, max_agent_replans=1, sdk_retries=1)
                await react_client._underlying_client._ensure_initialized()

                if self.use_native_proxy:
                    fault_proxy.set_fault("f1_response_drop")
                else:
                    self.toxiproxy.setup_f1_response_drop(self.proxy_name, timeout_ms=10)

                op_id = f"f1_react_tx_{uuid.uuid4().hex[:6]}"
                res = await react_client.execute_transfer(amount=150.0, recipient="Charlie", memo=op_id)
                await asyncio.sleep(0.05)
                eval_res = oracle.evaluate_task(op_id, res.success, res.error_message, res.attempts_made)
                f1_react_results.append(eval_res)
                details.append(eval_res)
                await react_client._underlying_client.close()

            # Clean up toxic
            if self.use_native_proxy:
                fault_proxy.clear_fault()
            else:
                self.toxiproxy.clear_f1_response_drop(self.proxy_name)

            # -------------------------------------------------------------
            # Compute Statistics
            # -------------------------------------------------------------
            base_succ = sum(1 for r in baseline_results if r.client_reported_success) / len(baseline_results)
            base_mult = sum(r.backend_multiplicity for r in baseline_results) / len(baseline_results)
            base_dups = sum(1 for r in baseline_results if r.has_duplicate)

            sdk_mult = sum(r.backend_multiplicity for r in f1_sdk_results) / len(f1_sdk_results)
            sdk_dups = sum(1 for r in f1_sdk_results if r.has_duplicate)
            sdk_phantoms = sum(1 for r in f1_sdk_results if r.is_phantom_failure)

            react_mult = sum(r.backend_multiplicity for r in f1_react_results) / len(f1_react_results)
            react_dups = sum(1 for r in f1_react_results if r.has_duplicate)
            react_phantoms = sum(1 for r in f1_react_results if r.is_phantom_failure)

            # Hypothesis H1: Multiplicity > 1 or duplicates > 0 under response loss after commit
            h1_confirmed = (sdk_mult > 1.0 or react_mult > 1.0 or sdk_dups > 0 or react_dups > 0)
            verdict = "PASSED (Gate G0 confirmed: stack duplicates/multiplies under F1)" if h1_confirmed else "FAILED (K1 triggered: no duplicates or multiplicity observed)"

            return G0ExperimentSummary(
                trials_per_condition=num_trials,
                baseline_success_rate=base_succ,
                baseline_multiplicity=base_mult,
                baseline_duplicates=base_dups,
                f1_sdk_retry_multiplicity=sdk_mult,
                f1_sdk_retry_duplicates=sdk_dups,
                f1_sdk_phantom_failures=sdk_phantoms,
                f1_react_multiplicity=react_mult,
                f1_react_duplicates=react_dups,
                f1_react_phantom_failures=react_phantoms,
                h1_confirmed=h1_confirmed,
                verdict=verdict,
                details=details,
            )

        finally:
            if fault_proxy:
                await fault_proxy.close()
            if fault_srv:
                fault_srv.should_exit = True
            if fault_task:
                await fault_task
            if not self.use_native_proxy:
                self.toxiproxy.delete_proxy(self.proxy_name)
            uvicorn_server.should_exit = True
            await server_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    runner = G0Runner()
    summary = asyncio.run(runner.run_experiment(num_trials=5))
    print("\n" + "=" * 60)
    print("GATE G0 EXPERIMENT SUMMARY REPORT")
    print("=" * 60)
    print(f"Trials per condition: {summary.trials_per_condition}")
    print(f"Baseline Success Rate: {summary.baseline_success_rate * 100:.1f}%")
    print(f"Baseline Multiplicity: {summary.baseline_multiplicity:.2f}")
    print(f"Baseline Duplicates:   {summary.baseline_duplicates}")
    print("-" * 60)
    print(f"F1 (SDK Retry) Multiplicity:   {summary.f1_sdk_retry_multiplicity:.2f}")
    print(f"F1 (SDK Retry) Duplicates:     {summary.f1_sdk_retry_duplicates}")
    print(f"F1 (SDK Retry) Phantom Errors: {summary.f1_sdk_phantom_failures}")
    print("-" * 60)
    print(f"F1 (ReAct Agent) Multiplicity: {summary.f1_react_multiplicity:.2f}")
    print(f"F1 (ReAct Agent) Duplicates:   {summary.f1_react_duplicates}")
    print(f"F1 (ReAct Agent) Phantom Errors: {summary.f1_react_phantom_failures}")
    print("-" * 60)
    print(f"Hypothesis H1 Confirmed: {summary.h1_confirmed}")
    print(f"Verdict: {summary.verdict}")
    print("=" * 60)
