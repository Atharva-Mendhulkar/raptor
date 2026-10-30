"""
Tests for RAPTOR CLI and Web UI Dashboard:
- CLI argument parsing
- Web UI HTML rendering
- /api/run-experiment endpoint
"""
import pytest
from starlette.testclient import TestClient
from raptor.ui.app import create_ui_app


def test_ui_dashboard_rendering():
    """Verify that the web dashboard renders HTML with status 200."""
    app = create_ui_app()
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "RAPTOR | PartitionGuard Dashboard" in response.text
    assert "Run Live Fault F1 Test" in response.text
    assert "Run Matrix Benchmark" in response.text


@pytest.mark.asyncio
async def test_ui_api_experiment_endpoint():
    """Verify that the /api/run-experiment endpoint returns unprotected vs guarded comparison."""
    app = create_ui_app()
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/run-experiment")
        assert resp.status_code == 200
        data = resp.json()

        assert "unprotected" in data
        assert "guarded" in data
        # Unprotected stack under F1 has duplicates
        assert data["unprotected"]["commits"] == 2
        assert data["unprotected"]["multiplicity"] == 2.0
        # Guarded stack has exactly 1 commit
        assert data["guarded"]["commits"] == 1
        assert data["guarded"]["multiplicity"] == 1.0
