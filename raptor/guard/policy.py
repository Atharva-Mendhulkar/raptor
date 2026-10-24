"""
Tool policy configuration and registry for RAPTOR Guard.
Fulfills Requirement G2:
'Per-tool policy file: effectful, idempotent, key policy, dedup window, status-check tool, retry allowed.
Acceptance criterion: Invalid policy rejected at start; unknown tools pass through with a warning.'
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional
import yaml
from pydantic import BaseModel, Field, ValidationError

from raptor.common.models import ToolPolicy

logger = logging.getLogger(__name__)


class ToolPolicyDefinition(BaseModel):
    effectful: bool = True
    idempotent: bool = False
    idempotency_fields: List[str] = Field(default_factory=lambda: ["recipient", "amount"])
    dedup_window_seconds: float = 60.0
    scope: str = "default"
    status_check_tool: Optional[str] = None
    allow_retry: bool = True
    min_service_time_seconds: float = 0.05


class GuardPolicyConfig(BaseModel):
    version: str = "1.0"
    tools: Dict[str, ToolPolicyDefinition] = Field(default_factory=dict)

    @classmethod
    def from_yaml_file(cls, path: str) -> "GuardPolicyConfig":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Policy file not found: {path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                raise ValueError("Policy file must contain a YAML dictionary mapping")
            return cls(**raw)
        except (yaml.YAMLError, ValidationError) as e:
            raise ValueError(f"Invalid tool policy specification in {path}: {e}") from e

    def get_tool_policy(self, tool_name: str) -> Optional[ToolPolicy]:
        """
        Retrieves ToolPolicy for a tool name.
        If unknown, logs a warning and returns a default non-effectful pass-through policy.
        """
        if tool_name not in self.tools:
            logger.warning(
                f"Tool '{tool_name}' not defined in policy. Passing through without dedup."
            )
            return ToolPolicy(
                tool_name=tool_name,
                is_effectful=False,
                idempotency_fields=[],
                scope="unknown",
            )

        d = self.tools[tool_name]
        return ToolPolicy(
            tool_name=tool_name,
            is_effectful=d.effectful,
            idempotency_fields=d.idempotency_fields,
            dedup_window_seconds=d.dedup_window_seconds,
            scope=d.scope,
            status_check_tool=d.status_check_tool,
            allow_retry=d.allow_retry,
            min_service_time_seconds=d.min_service_time_seconds,
        )
