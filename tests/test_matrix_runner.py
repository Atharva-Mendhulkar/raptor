"""
Tests for MatrixRunner:
- Verification of submatrix execution (raw_mcp, react_agent under clean and F1)
- Proves Guard reduces multiplicity to 1.00 across adapters under F1
- Verifies markdown table generation
"""
import pytest
from raptor.harness.runner_matrix import MatrixRunner


@pytest.mark.asyncio
async def test_matrix_runner_f1_comparison():
    """Run a focused 2x2 matrix comparing raw_mcp and react_agent under clean and F1."""
    runner = MatrixRunner(server_port=8945, fault_port=8946, guard_port=8947, seed=77)

    report = await runner.run_matrix(
        adapters=["raw_mcp", "react_agent"],
        faults=["none", "f1_response_drop"],
        modes=[False, True],
        trials_per_cell=2,
    )

    assert len(report.cells) == 8  # 2 adapters * 2 faults * 2 modes = 8 cells

    # Find the cell results
    cells_by_key = {(c.adapter_name, c.fault_name, c.guarded): c for c in report.cells}

    # 1. Clean run without guard: 100% success, multiplicity 1.0
    clean_unprot = cells_by_key[("raw_mcp", "none", False)]
    assert clean_unprot.multiplicity == 1.0
    assert clean_unprot.duplicate_rate == 0.0

    # 2. F1 run without guard (raw_mcp): multiplicity 2.0 because max_retries=1 duplicates backend effect
    f1_raw_unprot = cells_by_key[("raw_mcp", "f1_response_drop", False)]
    assert f1_raw_unprot.multiplicity == 2.0
    assert f1_raw_unprot.duplicate_rate == 1.0

    # 3. F1 run WITH guard (raw_mcp): multiplicity drops to 1.00, 0 duplicates!
    f1_raw_guard = cells_by_key[("raw_mcp", "f1_response_drop", True)]
    assert f1_raw_guard.multiplicity == 1.0
    assert f1_raw_guard.duplicate_rate == 0.0

    # 4. F1 run without guard (react_agent): compounded multiplicity 4.0 (2 replans x 2 SDK retries)
    f1_react_unprot = cells_by_key[("react_agent", "f1_response_drop", False)]
    assert f1_react_unprot.multiplicity == 4.0
    assert f1_react_unprot.duplicate_rate == 1.0

    # 5. F1 run WITH guard (react_agent): multiplicity drops to 1.00, 0 duplicates!
    f1_react_guard = cells_by_key[("react_agent", "f1_response_drop", True)]
    assert f1_react_guard.multiplicity == 1.0
    assert f1_react_guard.duplicate_rate == 0.0

    # 6. Verify Markdown table formatting
    md = report.to_markdown_table()
    assert "| Stack Adapter | Fault Mode |" in md
    assert "raw_mcp" in md
    assert "react_agent" in md
