"""Side-effect-free executor ports for the generic workflow engine.

Importing this package does not import a production runtime, load plugins, open
a database, or construct a provider client. Every executable capability is
supplied explicitly by the scheduler's composition root.
"""

from ..artifacts import BoundArtifact, ProducedArtifact
from .base import (
    ExecutionContext,
    ExecutionControl,
    ExecutionOutcome,
    ExecutionResult,
    Executor,
    ExecutorDescriptor,
    blocked_result,
    cancelled_result,
    enforce_output_contract,
)
from .codex_sdk import (
    CodexClient,
    CodexOutputCollector,
    CodexRequest,
    CodexResponse,
    CodexSDKExecutor,
)
from .deterministic import DeterministicExecutor
from .declared_files import (
    DECLARED_OUTPUT_FILES_METADATA_KEY,
    DeclaredFileOutputCollector,
    DeclaredOutputCollectionError,
    declared_output_files,
)
from .human import HumanExecutor
from .legacy_stage import LegacyStageExecutor
from .plugin import PluginExecutor
from .registry import ExecutorRegistry

__all__ = [
    "CodexClient",
    "CodexOutputCollector",
    "CodexRequest",
    "CodexResponse",
    "CodexSDKExecutor",
    "DECLARED_OUTPUT_FILES_METADATA_KEY",
    "DeclaredFileOutputCollector",
    "DeclaredOutputCollectionError",
    "DeterministicExecutor",
    "BoundArtifact",
    "ExecutionContext",
    "ExecutionControl",
    "ExecutionOutcome",
    "ExecutionResult",
    "Executor",
    "ExecutorDescriptor",
    "ExecutorRegistry",
    "HumanExecutor",
    "LegacyStageExecutor",
    "PluginExecutor",
    "ProducedArtifact",
    "blocked_result",
    "cancelled_result",
    "declared_output_files",
    "enforce_output_contract",
]
