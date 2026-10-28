"""Purely local implementations for the built-in generic example workflow.

The handlers in this module are deliberately small and deterministic.  They
consume only artifacts supplied through :class:`ExecutionContext` and create
files only below the attempt's ``output_root``.  No clock, network, process, or
environment state participates in their output.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .artifacts import BoundArtifact, ProducedArtifact
from .executors.base import ExecutionContext, ExecutionOutcome, ExecutionResult


NORMALIZE_REQUEST_IMPLEMENTATION = "generic.examples.normalize-request.v1"
ACTION_PLAN_IMPLEMENTATION = "generic.examples.action-plan.v1"
SUMMARY_IMPLEMENTATION = "generic.examples.summary.v1"


def _artifact_payload(context: ExecutionContext, artifact_id: str) -> Any:
    """Read one declared context artifact without consulting ambient state."""

    artifact = context.inputs[artifact_id]
    if not isinstance(artifact, BoundArtifact):
        raise TypeError(f"context input is not a BoundArtifact: {artifact_id}")
    if artifact.value is not None:
        return artifact.value
    if not artifact.uri:
        raise ValueError(f"context input has neither an inline value nor a sealed URI: {artifact_id}")
    workspace = Path(os.path.abspath(context.workspace_root)).resolve(strict=True)
    source = Path(os.path.abspath(context.workspace_root / Path(artifact.uri)))
    resolved = source.resolve(strict=True)
    if resolved == workspace or workspace not in resolved.parents:
        raise ValueError(f"context input escaped the workspace: {artifact_id}")
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != artifact.sha256.lower():
        raise ValueError(f"context input digest changed: {artifact_id}")
    if artifact.size_bytes is not None and len(payload) != artifact.size_bytes:
        raise ValueError(f"context input size changed: {artifact_id}")
    return json.loads(payload.decode("utf-8"))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"request field must be text: {field}")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"request field cannot be empty: {field}")
    return normalized


def _text_sequence(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"request field must be an array of text values: {field}")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, field)
        if text not in seen:
            normalized.append(text)
            seen.add(text)
    return normalized


def _write_json(output_root: Path, filename: str, value: Mapping[str, Any]) -> Path:
    target = output_root / filename
    target.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def normalize_request(context: ExecutionContext) -> ExecutionResult:
    """Normalize a compact generic request into canonical JSON."""

    raw = _artifact_payload(context, "request")
    if not isinstance(raw, Mapping):
        raise TypeError("request must be a JSON object")
    goal = _text(raw.get("goal"), "goal")
    actions = _text_sequence(raw.get("actions"), "actions")
    constraints = _text_sequence(raw.get("constraints"), "constraints")
    if not actions:
        actions = [goal]
    normalized = {
        "actions": actions,
        "constraints": constraints,
        "goal": goal,
    }
    target = _write_json(context.output_root, "normalized-request.json", normalized)
    return ExecutionResult(
        ExecutionOutcome.COMPLETED,
        produced_artifacts=(ProducedArtifact("normalized_request", target, "application/json"),),
    )


def build_action_plan(context: ExecutionContext) -> ExecutionResult:
    """Convert normalized actions into an ordered, portable action plan."""

    normalized = _artifact_payload(context, "normalized_request")
    if not isinstance(normalized, Mapping):
        raise TypeError("normalized request must be a JSON object")
    goal = _text(normalized.get("goal"), "goal")
    actions = _text_sequence(normalized.get("actions"), "actions")
    constraints = _text_sequence(normalized.get("constraints"), "constraints")
    if not actions:
        raise ValueError("normalized request must contain at least one action")
    plan = {
        "actions": [
            {
                "action": action,
                "order": index,
            }
            for index, action in enumerate(actions, start=1)
        ],
        "constraints": constraints,
        "goal": goal,
    }
    target = _write_json(context.output_root, "action-plan.json", plan)
    return ExecutionResult(
        ExecutionOutcome.COMPLETED,
        produced_artifacts=(ProducedArtifact("action_plan", target, "application/json"),),
    )


def render_summary(context: ExecutionContext) -> ExecutionResult:
    """Render a stable Markdown summary from a sealed action plan artifact."""

    plan = _artifact_payload(context, "action_plan")
    if not isinstance(plan, Mapping):
        raise TypeError("action plan must be a JSON object")
    goal = _text(plan.get("goal"), "goal")
    raw_actions = plan.get("actions")
    if isinstance(raw_actions, (str, bytes)) or not isinstance(raw_actions, Sequence):
        raise TypeError("action plan actions must be an array")
    actions: list[str] = []
    for item in raw_actions:
        if not isinstance(item, Mapping):
            raise TypeError("each action plan entry must be an object")
        actions.append(_text(item.get("action"), "action"))
    if not actions:
        raise ValueError("action plan must contain at least one action")
    constraints = _text_sequence(plan.get("constraints"), "constraints")

    lines = ["# Workflow Summary", "", "## Goal", "", goal, "", "## Actions", ""]
    lines.extend(f"{index}. {action}" for index, action in enumerate(actions, start=1))
    lines.extend(["", "## Constraints", ""])
    lines.extend((f"- {constraint}" for constraint in constraints) if constraints else ("- None declared",))
    target = context.output_root / "summary.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return ExecutionResult(
        ExecutionOutcome.COMPLETED,
        produced_artifacts=(ProducedArtifact("summary", target, "text/markdown"),),
    )


def generic_example_implementations() -> Mapping[str, Any]:
    """Return the explicit allowlist consumed by ``DeterministicExecutor``."""

    return MappingProxyType(
        {
            ACTION_PLAN_IMPLEMENTATION: build_action_plan,
            NORMALIZE_REQUEST_IMPLEMENTATION: normalize_request,
            SUMMARY_IMPLEMENTATION: render_summary,
        }
    )


__all__ = [
    "ACTION_PLAN_IMPLEMENTATION",
    "NORMALIZE_REQUEST_IMPLEMENTATION",
    "SUMMARY_IMPLEMENTATION",
    "build_action_plan",
    "generic_example_implementations",
    "normalize_request",
    "render_summary",
]
