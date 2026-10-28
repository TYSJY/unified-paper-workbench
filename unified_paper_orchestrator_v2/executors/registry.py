from __future__ import annotations

from .base import Executor
from .canonical import CanonicalCommitExecutor
from .codex_cli_relay import CodexCLIRelayReviewExecutor
from .codex_sdk import CodexSDKExecutor
from .deterministic import DeterministicExecutor
from .human import BrowserExecutor, HumanExecutor
from .legacy import LegacyCodexAutomationExecutor, LegacyOrchestratorExecutor
from .openai_api import OpenAIAPIExecutor
from .openai_relay import OpenAICompatibleRelayReviewExecutor
from .openai_review import OpenAIReviewExecutor


class ExecutorRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Executor] = {}

    def register(self, executor: Executor) -> None:
        if executor.name in self._items:
            raise ValueError(f"duplicate executor: {executor.name}")
        self._items[executor.name] = executor

    def get(self, name: str) -> Executor:
        try:
            return self._items[name]
        except KeyError as exc:
            raise ValueError(f"unknown executor: {name}") from exc

    def describe(self) -> list[dict[str, object]]:
        return [self._items[name].descriptor().to_dict() for name in sorted(self._items)]


def build_default_registry(
    *,
    codex_sdk_executor: CodexSDKExecutor | None = None,
) -> ExecutorRegistry:
    registry = ExecutorRegistry()
    registry.register(DeterministicExecutor())
    registry.register(HumanExecutor())
    registry.register(BrowserExecutor())
    registry.register(OpenAIAPIExecutor())
    registry.register(OpenAIReviewExecutor())
    registry.register(OpenAICompatibleRelayReviewExecutor())
    registry.register(CodexCLIRelayReviewExecutor())
    registry.register(CanonicalCommitExecutor())
    registry.register(codex_sdk_executor or CodexSDKExecutor())
    registry.register(LegacyOrchestratorExecutor())
    registry.register(LegacyCodexAutomationExecutor())
    return registry
