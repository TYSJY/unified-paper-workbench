from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from ..contracts import TaskEnvelope, TaskResult


@dataclass(frozen=True)
class ExecutorDescriptor:
    name: str
    available: bool
    kind: str
    detail: str = ""
    side_effecting: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Executor(Protocol):
    name: str

    def descriptor(self) -> ExecutorDescriptor: ...

    def execute(self, task: TaskEnvelope) -> TaskResult: ...

