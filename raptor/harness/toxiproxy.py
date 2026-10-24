"""Toxiproxy controller for network fault injection."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger(__name__)


class ToxiproxyController:
    """Client for controlling Toxiproxy daemon via its REST API (default http://127.0.0.1:8474)."""

    def __init__(self, api_url: str = "http://127.0.0.1:8474"):
        self.api_url = api_url.rstrip("/")
        self.client = httpx.Client(base_url=self.api_url, timeout=5.0)

    def is_reachable(self) -> bool:
        """Check if Toxiproxy daemon is reachable."""
        try:
            resp = self.client.get("/version")
            return resp.status_code == 200
        except Exception as exc:
            logger.debug(f"Toxiproxy unreachable: {exc}")
            return False

    def create_proxy(self, name: str, listen: str, upstream: str) -> Dict[str, Any]:
        """Create a new TCP proxy."""
        # Clean up existing if present
        self.delete_proxy(name)

        payload = {
            "name": name,
            "listen": listen,
            "upstream": upstream,
            "enabled": True,
        }
        resp = self.client.post("/proxies", json=payload)
        resp.raise_for_status()
        return resp.json()

    def delete_proxy(self, name: str) -> bool:
        """Delete an existing proxy by name."""
        try:
            resp = self.client.delete(f"/proxies/{name}")
            return resp.status_code in (200, 204)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise

    def add_toxic(
        self,
        proxy_name: str,
        name: str,
        toxic_type: str,
        stream: str = "downstream",
        attributes: Optional[Dict[str, Any]] = None,
        toxicity: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Add a toxic to intercept traffic.
        stream: 'downstream' (server -> client, response) or 'upstream' (client -> server, request)
        """
        payload = {
            "name": name,
            "type": toxic_type,
            "stream": stream,
            "toxicity": toxicity,
            "attributes": attributes or {},
        }
        resp = self.client.post(f"/proxies/{proxy_name}/toxics", json=payload)
        resp.raise_for_status()
        return resp.json()

    def remove_toxic(self, proxy_name: str, toxic_name: str) -> bool:
        """Remove a toxic from a proxy."""
        try:
            resp = self.client.delete(f"/proxies/{proxy_name}/toxics/{toxic_name}")
            return resp.status_code in (200, 204)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return False
            raise

    def setup_f1_response_drop(self, proxy_name: str, timeout_ms: int = 10) -> Dict[str, Any]:
        """
        Fault F1: Response lost after commit.
        Injects a 'reset_peer' toxic on downstream with a small timeout.
        Allows request to reach upstream and commit, but cuts TCP socket on return.
        """
        self.remove_toxic(proxy_name, "f1_response_drop")
        return self.add_toxic(
            proxy_name=proxy_name,
            name="f1_response_drop",
            toxic_type="reset_peer",
            stream="downstream",
            attributes={"timeout": timeout_ms},
            toxicity=1.0,
        )

    def clear_f1_response_drop(self, proxy_name: str) -> bool:
        """Clear Fault F1 from the proxy."""
        return self.remove_toxic(proxy_name, "f1_response_drop")

    def setup_f3_latency_jitter(
        self,
        proxy_name: str,
        latency_ms: int = 100,
        jitter_ms: int = 20,
    ) -> Dict[str, Any]:
        """
        Fault F3: Added latency and jitter.
        Applies delay to simulate slow / degraded network links.
        """
        self.remove_toxic(proxy_name, "f3_latency")
        return self.add_toxic(
            proxy_name=proxy_name,
            name="f3_latency",
            toxic_type="latency",
            stream="downstream",
            attributes={"latency": latency_ms, "jitter": jitter_ms},
            toxicity=1.0,
        )

    def clear_f3_latency_jitter(self, proxy_name: str) -> bool:
        """Clear Fault F3 from the proxy."""
        return self.remove_toxic(proxy_name, "f3_latency")

    def setup_f4_timeout(
        self,
        proxy_name: str,
        timeout_ms: int = 5000,
    ) -> Dict[str, Any]:
        """
        Fault F4: Connection / Partition timeout.
        Stops all data transmission, causing client read timeouts.
        """
        self.remove_toxic(proxy_name, "f4_timeout")
        return self.add_toxic(
            proxy_name=proxy_name,
            name="f4_timeout",
            toxic_type="timeout",
            stream="downstream",
            attributes={"timeout": timeout_ms},
            toxicity=1.0,
        )

    def clear_f4_timeout(self, proxy_name: str) -> bool:
        """Clear Fault F4 from the proxy."""
        return self.remove_toxic(proxy_name, "f4_timeout")

    def setup_connection_reset(
        self,
        proxy_name: str,
        stream: str = "upstream",
    ) -> Dict[str, Any]:
        """
        Immediate connection reset (TCP RST).
        """
        self.remove_toxic(proxy_name, "conn_reset")
        return self.add_toxic(
            proxy_name=proxy_name,
            name="conn_reset",
            toxic_type="reset_peer",
            stream=stream,
            attributes={"timeout": 0},
            toxicity=1.0,
        )

    def clear_connection_reset(self, proxy_name: str) -> bool:
        """Clear connection reset toxic."""
        return self.remove_toxic(proxy_name, "conn_reset")

    def setup_f7_bandwidth_cap(
        self,
        proxy_name: str,
        rate_kbps: int = 50,
    ) -> Dict[str, Any]:
        """
        Fault F7: Bandwidth cap.
        Throttles connection to rate_kbps (kilobytes per second).
        """
        self.remove_toxic(proxy_name, "f7_bandwidth")
        return self.add_toxic(
            proxy_name=proxy_name,
            name="f7_bandwidth",
            toxic_type="bandwidth",
            stream="downstream",
            attributes={"rate": rate_kbps},
            toxicity=1.0,
        )

    def clear_f7_bandwidth_cap(self, proxy_name: str) -> bool:
        """Clear Fault F7 from the proxy."""
        return self.remove_toxic(proxy_name, "f7_bandwidth")

    def reset_all(self) -> None:
        """Reset state across all proxies."""
        try:
            resp = self.client.get("/proxies")
            if resp.status_code == 200:
                for name in resp.json().keys():
                    self.delete_proxy(name)
        except Exception:
            pass
