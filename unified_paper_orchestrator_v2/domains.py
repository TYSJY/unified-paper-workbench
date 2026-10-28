from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageDefinition:
    name: str
    actions: frozenset[str]
    default_executor: str
    approval_actions: frozenset[str] = frozenset()

    def validate_action(self, action: str) -> None:
        if action not in self.actions:
            raise ValueError(f"unsupported action for stage {self.name}: {action}")


STAGES = {
    "system": StageDefinition(
        name="system",
        actions=frozenset(
            {
                "system.noop",
                "artifact.verify",
                "artifact.canonical_commit",
                "legacy.plan",
                "legacy.apply",
                "legacy.automation.status",
                "legacy.automation.start",
                "legacy.automation.pause",
            }
        ),
        default_executor="deterministic",
        approval_actions=frozenset(
            {
                "artifact.canonical_commit",
                "legacy.apply",
                "legacy.automation.start",
                "legacy.automation.pause",
            }
        ),
    ),
    "writing": StageDefinition(
        name="writing",
        actions=frozenset(
            {
                "writing.idea",
                "writing.references",
                "writing.draft",
                "writing.review",
                "writing.review_round_1.canary",
                "writing.review_round_1.compatible_relay_canary",
                "writing.review_round_1.qilin_codex_cli_canary",
                "writing.review_round_1.codex_sdk_profile_canary",
                "artifact.verify",
            }
        ),
        default_executor="codex_sdk",
        approval_actions=frozenset(
            {
                "writing.review_round_1.compatible_relay_canary",
                "writing.review_round_1.qilin_codex_cli_canary",
                "writing.review_round_1.codex_sdk_profile_canary",
            }
        ),
    ),
    "figures": StageDefinition(
        name="figures",
        actions=frozenset({"figures.plan", "figures.generate", "figures.package", "artifact.verify"}),
        default_executor="codex_sdk",
    ),
    "journal_author": StageDefinition(
        name="journal_author",
        actions=frozenset({"journal.select", "journal.package", "artifact.verify"}),
        default_executor="openai_api",
    ),
    "submission": StageDefinition(
        name="submission",
        actions=frozenset({"submission.prepare", "submission.monitor", "submission.execute", "artifact.verify"}),
        default_executor="browser",
        approval_actions=frozenset({"submission.execute"}),
    ),
}


def stage_definition(name: str) -> StageDefinition:
    try:
        return STAGES[name]
    except KeyError as exc:
        raise ValueError(f"unknown stage: {name}") from exc
