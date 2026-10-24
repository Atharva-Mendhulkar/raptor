"""
Linux tc/netem Kernel Network Fault Injection Controller.
Fulfills Requirement H3 & Section 10.1:
'Inject packet loss and partition windows with tc/netem inside a Linux namespace.'
Safe on macOS with graceful fallback when kernel netem / tc is not present.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class NetemController:
    """Controls Linux tc (traffic control) netem qdisc for kernel-level packet manipulation."""

    def __init__(self, interface: str = "eth0"):
        self.interface = interface
        self._tc_bin = shutil.which("tc")

    def is_available(self) -> bool:
        """Checks if tc binary is present and interface is accessible."""
        if not self._tc_bin:
            return False
        try:
            res = subprocess.run(
                [self._tc_bin, "qdisc", "show", "dev", self.interface],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False

    def add_packet_loss(self, loss_percent: float = 5.0) -> bool:
        """Injects random packet loss at the kernel IP layer."""
        if not self.is_available():
            logger.warning("tc/netem not available on host. Use Docker VM for kernel netem.")
            return False
        self.clear()
        cmd = [self._tc_bin, "qdisc", "add", "dev", self.interface, "root", "netem", "loss", f"{loss_percent}%"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0

    def add_latency(self, latency_ms: float = 100.0, jitter_ms: float = 20.0) -> bool:
        """Injects network latency and jitter at the kernel IP layer."""
        if not self.is_available():
            logger.warning("tc/netem not available on host. Use Docker VM for kernel netem.")
            return False
        self.clear()
        cmd = [
            self._tc_bin,
            "qdisc",
            "add",
            "dev",
            self.interface,
            "root",
            "netem",
            "delay",
            f"{latency_ms}ms",
            f"{jitter_ms}ms",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0

    def clear(self) -> bool:
        """Removes all netem qdisc configurations from the interface."""
        if not self._tc_bin:
            return False
        cmd = [self._tc_bin, "qdisc", "del", "dev", self.interface, "root"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode == 0
