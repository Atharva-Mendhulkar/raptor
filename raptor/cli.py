"""
RAPTOR Unified Command-Line Interface.
Zero unrequested abstractions: stdlib argparse commands for UI, benchmark, and scenario runs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uvicorn

from raptor.harness.runner_matrix import MatrixRunner
from raptor.harness.scenario import ScenarioConfig, ScenarioRunner
from raptor.ui.app import create_ui_app


def main():
    parser = argparse.ArgumentParser(
        prog="raptor",
        description="RAPTOR / PartitionGuard: Fault-injection harness & sidecar proxy",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: ui
    ui_parser = subparsers.add_parser("ui", help="Launch the lightweight web dashboard")
    ui_parser.add_argument("--port", type=int, default=8000, help="Port to bind dashboard (default: 8000)")
    ui_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")

    # Command: bench
    bench_parser = subparsers.add_parser("bench", help="Run the matrix benchmark across adapters and faults")
    bench_parser.add_argument("--adapters", nargs="+", default=["raw_mcp", "react_agent"], help="Adapters to benchmark")
    bench_parser.add_argument("--faults", nargs="+", default=["none", "f1_response_drop"], help="Faults to inject")
    bench_parser.add_argument("--trials", type=int, default=3, help="Trials per cell (default: 3)")

    # Command: scenario
    scenario_parser = subparsers.add_parser("scenario", help="Execute a YAML-defined fault scenario")
    scenario_parser.add_argument("file", type=str, help="Path to scenario YAML file")

    args = parser.parse_args()

    if args.command == "ui":
        print(f"🚀 Starting RAPTOR Dashboard at http://{args.host}:{args.port}")
        app = create_ui_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")

    elif args.command == "bench":
        async def _run_bench():
            runner = MatrixRunner()
            print(f"Running benchmark matrix: adapters={args.adapters}, faults={args.faults}, trials={args.trials}...")
            report = await runner.run_matrix(
                adapters=args.adapters,
                faults=args.faults,
                modes=[False, True],
                trials_per_cell=args.trials,
            )
            print("\n" + report.to_markdown_table() + "\n")

        asyncio.run(_run_bench())

    elif args.command == "scenario":
        async def _run_scenario():
            cfg = ScenarioConfig.from_yaml_file(args.file)
            runner = ScenarioRunner(cfg)
            res = await runner.run()
            if res.report:
                print("\n" + res.report.to_ascii_table() + "\n")
            else:
                print(f"Multiplicity: {res.multiplicity:.2f} | Duplicates: {res.duplicate_count}")

        asyncio.run(_run_scenario())

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
