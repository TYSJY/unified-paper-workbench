from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator.config import WRITING_ROOT  # noqa: E402
from unified_paper_orchestrator_v2.executors.openai_review import (  # noqa: E402
    OpenAIReviewExecutor,
)
from unified_paper_orchestrator_v2.executors.registry import (  # noqa: E402
    ExecutorRegistry,
)
from unified_paper_orchestrator_v2.gateway import ExecutionGateway  # noqa: E402
from unified_paper_orchestrator_v2.staging import StagingStore  # noqa: E402


PAPER_ID = "2026-07-02_001_reconfiguration-debt-decoding"
OFFLINE_MODEL = "offline-golden-replay-v1"
KNOWN_PROMPT_SHA256 = "faf7e03dc571fd7eaf5ff0dcec38e30fcfd8cfa732434372384378cb1ee13830"
KNOWN_REVIEW_SHA256 = "2819f57cba1e2392666299dbfe9d89fb1f5ba63456f23439f47b8649399c7156"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fixture_paths(writing_root: Path) -> dict[str, Path]:
    root = Path(writing_root).resolve()
    paths = {
        "idea": root / "ideas" / f"{PAPER_ID}.md",
        "references": root / "refs" / PAPER_ID / "references.md",
        "literature_map": root / "refs" / PAPER_ID / "literature_map.md",
        "novelty_check": root / "refs" / PAPER_ID / "novelty_check.md",
        "draft_v3": root / "papers" / PAPER_ID / "draft_v3.tex",
        "source_prompt": root / "prompts" / "07_review_round_1.md",
        "historical_review": root / "papers" / PAPER_ID / "review_round_1.md",
    }
    for role, path in paths.items():
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"Fixture path escapes the writing root: {role}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(f"Required replay fixture is missing: {role}")
        paths[role] = resolved
    return paths


def _snapshot(paths: dict[str, Path]) -> dict[str, str]:
    return {role: _sha256(path.read_bytes()) for role, path in paths.items()}


class _OfflineResponses:
    def __init__(self, historical_review: str) -> None:
        self._historical_review = historical_review
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        instructions = str(kwargs.get("instructions") or "")
        input_text = str(kwargs.get("input") or "")
        request_text = f"{instructions}\n{input_text}"
        historical_review_as_input = self._historical_review in request_text
        if historical_review_as_input:
            raise RuntimeError("Historical review was incorrectly included in the replay request.")
        self.calls.append(
            {
                "model": str(kwargs.get("model") or ""),
                "store": bool(kwargs.get("store", True)),
                "max_output_tokens": int(kwargs.get("max_output_tokens") or 0),
                "instructions_sha256": _sha256(instructions.encode("utf-8")),
                "input_sha256": _sha256(input_text.encode("utf-8")),
                "historical_review_as_input": historical_review_as_input,
            }
        )
        return SimpleNamespace(
            id=f"offline_replay_{KNOWN_REVIEW_SHA256[:16]}",
            status="completed",
            output_text=self._historical_review,
            # Deliberately synthetic positive usage: this is an injected,
            # offline transport replay and must still exercise the same
            # fail-closed usage-evidence gate as a live response.
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            incomplete_details=None,
        )


class _OfflineClient:
    def __init__(self, responses: _OfflineResponses) -> None:
        self.responses = responses


def _new_replay_dir(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = (Path(root).resolve() / stamp).resolve()
    target.mkdir(parents=True, exist_ok=False)
    return target


def _ledger_contains(ledger_path: Path, payload: bytes) -> bool:
    candidates = [ledger_path, ledger_path.with_name(f"{ledger_path.name}-wal")]
    return any(path.is_file() and payload in path.read_bytes() for path in candidates)


def _unexpected_outputs(replay_dir: Path, staging_dir: Path, ledger_path: Path) -> list[str]:
    allowed = {
        ledger_path.resolve(),
        ledger_path.with_name(f"{ledger_path.name}-wal").resolve(),
        ledger_path.with_name(f"{ledger_path.name}-shm").resolve(),
        (staging_dir / "review_round_1.md").resolve(),
        (staging_dir / "result.json").resolve(),
    }
    return sorted(
        str(path.relative_to(replay_dir))
        for path in replay_dir.rglob("*")
        if path.is_file() and path.resolve() not in allowed
    )


def run_replay(*, writing_root: Path, replay_root: Path) -> dict[str, Any]:
    fixture_paths = _fixture_paths(writing_root)
    before = _snapshot(fixture_paths)
    if before["source_prompt"] != KNOWN_PROMPT_SHA256:
        raise RuntimeError("The immutable Prompt 07 fixture does not match its known SHA256.")
    if before["historical_review"] != KNOWN_REVIEW_SHA256:
        raise RuntimeError("The historical review fixture does not match its known SHA256.")

    historical_bytes = fixture_paths["historical_review"].read_bytes()
    historical_review = historical_bytes.decode("utf-8")
    replay_dir = _new_replay_dir(replay_root)
    ledger_path = replay_dir / "gateway.sqlite3"
    staging_root = replay_dir / "staging"

    responses = _OfflineResponses(historical_review)
    registry = ExecutorRegistry()
    registry.register(OpenAIReviewExecutor(lambda _task: _OfflineClient(responses)))
    staging_store = StagingStore(staging_root, acl_hardener=None)
    gateway = ExecutionGateway(
        database_path=ledger_path,
        staging_root=staging_root,
        registry=registry,
        staging_store=staging_store,
        review_writing_root=Path(writing_root),
    )
    queued = gateway.submit_review_canary(
        PAPER_ID,
        model=OFFLINE_MODEL,
        writing_root=Path(writing_root),
    )
    completed = gateway.run_once(str(queued["task_id"]))
    after = _snapshot(fixture_paths)

    if completed is None or completed["status"] != "completed":
        raise RuntimeError("Offline replay did not complete successfully.")
    if before != after:
        raise RuntimeError("A source fixture changed during the offline replay.")
    if len(responses.calls) != 1:
        raise RuntimeError("Offline replay did not issue exactly one injected Responses call.")

    staging_dir = Path(completed["result"]["data"]["staging_dir"]).resolve()
    staged_review = staging_dir / "review_round_1.md"
    result_manifest = staging_dir / "result.json"
    if {path.name for path in staging_dir.iterdir()} != {"review_round_1.md", "result.json"}:
        raise RuntimeError("Offline replay staging contains unexpected artifacts.")
    if staged_review.read_bytes() != historical_bytes:
        raise RuntimeError("Staged replay output differs from the historical fake response.")
    ledger_records = {
        "tasks": gateway.store.list_tasks(limit=10),
        "events": gateway.store.list_events(limit=20),
    }
    ledger_text = json.dumps(ledger_records, ensure_ascii=False, sort_keys=True)
    review_probes = [
        historical_review[offset : offset + 256]
        for offset in range(0, len(historical_review), 256)
        if len(historical_review[offset : offset + 256]) == 256
    ]
    historical_review_in_ledger = (
        _ledger_contains(ledger_path, historical_bytes)
        or "output_text" in ledger_text
        or any(probe in ledger_text for probe in review_probes)
    )
    if historical_review_in_ledger:
        raise RuntimeError("Historical review output leaked into the compact replay ledger.")

    unexpected = _unexpected_outputs(replay_dir, staging_dir, ledger_path)
    if unexpected:
        raise RuntimeError(f"Replay produced unexpected files: {unexpected}")
    ledger = gateway.store.summary()
    request = responses.calls[0]
    return {
        "schema_version": "offline_review_replay_v1",
        "status": "completed",
        "mode": "injected_fake_responses_no_network",
        "paper_id": PAPER_ID,
        "replay_dir": str(replay_dir),
        "ledger_path": str(ledger_path),
        "staging_dir": str(staging_dir),
        "task_id": completed["task_id"],
        "run_id": completed["run_id"],
        "source_integrity": {"unchanged": True, "sha256": before},
        "historical_review": {
            "sha256": before["historical_review"],
            "used_as": "fake_response_output_only",
            "present_in_request": request["historical_review_as_input"],
            "present_in_ledger": historical_review_in_ledger,
        },
        "request": {
            "count": 1,
            "model": request["model"],
            "store": request["store"],
            "max_output_tokens": request["max_output_tokens"],
            "instructions_sha256": request["instructions_sha256"],
            "input_sha256": request["input_sha256"],
        },
        "staged": {
            "files": ["review_round_1.md", "result.json"],
            "review_sha256": _sha256(staged_review.read_bytes()),
            "result_sha256": _sha256(result_manifest.read_bytes()),
            "canonical_write": completed["result"]["data"]["canonical_write"],
        },
        "ledger": {
            "task_counts": ledger["task_counts"],
            "event_count": ledger["event_count"],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the real review canary offline with the historical review as fake API output.",
    )
    parser.add_argument("--writing-root", type=Path, default=Path(WRITING_ROOT))
    parser.add_argument("--replay-root", type=Path, default=ROOT / "state" / "v2" / "replays")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_replay(writing_root=args.writing_root, replay_root=args.replay_root)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "offline_review_replay_v1",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
