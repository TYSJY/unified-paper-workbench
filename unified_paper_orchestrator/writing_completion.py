from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_PAPER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_PIPELINE_MARKER_RE = re.compile(
    r"(?m)^[ \t>`*+-]*PIPELINE_ID\s*=\s*([A-Za-z0-9][A-Za-z0-9._-]{0,199})\s*[` \t]*$"
)
_STEP_MARKER_RE = re.compile(r"(?m)^[ \t>`*+-]*STEP_DONE\s*=\s*([1-9]|[1-4][0-9]|50)\s*[` \t]*$")


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def expected_step_artifacts(root: Path, papers_dir: Path, paper_id: str, step_done: int) -> list[Path]:
    """Return the output contract for one completed writing step.

    These are artifact names, not prompt contents.  Keeping the contract in the
    controller avoids changing any of the user's tuned writing prompts.
    """

    if not _valid_paper_id(paper_id):
        return []
    outputs = {
        1: [root / "ideas" / f"{paper_id}.md"],
        2: [
            root / "refs" / paper_id / "references.md",
            root / "refs" / paper_id / "references.bib",
            root / "refs" / paper_id / "literature_map.md",
            root / "refs" / paper_id / "novelty_check.md",
        ],
        3: [papers_dir / paper_id / "draft_v0.tex"],
        4: [papers_dir / paper_id / "draft_v1.tex"],
        5: [papers_dir / paper_id / "draft_v2.tex"],
        6: [papers_dir / paper_id / "draft_v3.tex"],
        7: [papers_dir / paper_id / "review_round_1.md"],
        8: [papers_dir / paper_id / "draft_v4.tex"],
        9: [papers_dir / paper_id / "draft_v5.tex"],
        10: [papers_dir / paper_id / "draft_v6.tex"],
    }
    return outputs.get(step_done, [])


def fingerprint_step_artifacts(
    root: Path,
    papers_dir: Path,
    paper_id: str,
    step_done: int,
) -> list[dict[str, Any]] | None:
    paths = expected_step_artifacts(root, papers_dir, paper_id, step_done)
    if not paths:
        return None
    root_resolved = root.resolve()
    fingerprints: list[dict[str, Any]] = []
    for path in paths:
        fingerprint = _fingerprint_file(path, root_resolved)
        if fingerprint is None:
            return None
        fingerprints.append(fingerprint)
    return fingerprints


class CodexThreadCompletionReader:
    """Read terminal paper markers from the local Codex rollout store.

    A historical assistant message alone is not completion evidence.  The
    reader accepts only the latest ``task_complete`` event for the stored
    thread, and fails closed while a newer turn is open or the JSONL file is
    changing/corrupt.
    """

    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = (codex_home or default_codex_home()).resolve()
        self._path_cache: dict[str, Path] = {}
        self._result_cache: dict[tuple[str, int, int], dict[str, Any]] = {}

    def inspect(self, thread_id: str) -> dict[str, Any]:
        thread_id = str(thread_id or "").strip()
        if not _THREAD_ID_RE.fullmatch(thread_id):
            return {"status": "invalid_thread_id", "thread_id": thread_id}
        path = self._rollout_path(thread_id)
        if path is None:
            return {"status": "rollout_missing", "thread_id": thread_id}
        try:
            before = path.stat()
        except OSError:
            return {"status": "rollout_unreadable", "thread_id": thread_id}
        cache_key = (str(path), before.st_size, before.st_mtime_ns)
        cached = self._result_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        result = self._read_stable_rollout(path, thread_id, before.st_size, before.st_mtime_ns)
        if result.get("status") not in {"rollout_changing", "rollout_unreadable"}:
            self._result_cache = {cache_key: dict(result)}
        return result

    def _rollout_path(self, thread_id: str) -> Path | None:
        cached = self._path_cache.get(thread_id)
        if cached is not None and cached.is_file():
            return cached
        candidates: list[Path] = []
        for folder in ("sessions", "archived_sessions"):
            base = self.codex_home / folder
            if base.is_dir():
                candidates.extend(path for path in base.rglob(f"*-{thread_id}.jsonl") if path.is_file())
        if not candidates:
            return None
        try:
            candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
        except OSError:
            return None
        self._path_cache[thread_id] = candidates[0]
        return candidates[0]

    def _read_stable_rollout(
        self,
        path: Path,
        thread_id: str,
        expected_size: int,
        expected_mtime_ns: int,
    ) -> dict[str, Any]:
        terminal: dict[str, Any] | None = None
        newer_turn_open = False
        newer_turn_evidence: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line_number, raw in enumerate(handle, start=1):
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeError):
                        return {
                            "status": "rollout_malformed",
                            "thread_id": thread_id,
                            "line": line_number,
                        }
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    record_type = str(record.get("type") or "")
                    payload_type = str(payload.get("type") or "")
                    if terminal is not None:
                        boundary = ""
                        if record_type == "event_msg" and payload_type in {
                            "task_started",
                            "user_message",
                        }:
                            boundary = payload_type
                        elif record_type == "turn_context":
                            boundary = "turn_context"
                        elif (
                            record_type == "response_item"
                            and payload_type == "message"
                            and str(payload.get("role") or "") == "user"
                        ):
                            boundary = "response_item_user_message"
                        if boundary:
                            newer_turn_open = True
                            newer_turn_evidence = {
                                "record_type": record_type,
                                "payload_type": payload_type,
                                "turn_id": str(payload.get("turn_id") or ""),
                                "timestamp": str(record.get("timestamp") or ""),
                                "boundary": boundary,
                            }
                    if record.get("type") != "event_msg" or payload.get("type") != "task_complete":
                        continue
                    terminal = {
                        "timestamp": str(record.get("timestamp") or ""),
                        "turn_id": str(payload.get("turn_id") or ""),
                        "completed_at": payload.get("completed_at"),
                        "last_agent_message": str(payload.get("last_agent_message") or ""),
                    }
                    newer_turn_open = False
                    newer_turn_evidence = {}
        except (OSError, UnicodeError):
            return {"status": "rollout_unreadable", "thread_id": thread_id}
        try:
            after = path.stat()
        except OSError:
            return {"status": "rollout_unreadable", "thread_id": thread_id}
        if after.st_size != expected_size or after.st_mtime_ns != expected_mtime_ns:
            return {"status": "rollout_changing", "thread_id": thread_id}
        if terminal is None:
            return {"status": "task_complete_missing", "thread_id": thread_id}
        if newer_turn_open:
            return {
                "status": "newer_turn_open",
                "thread_id": thread_id,
                "newer_turn_evidence": newer_turn_evidence,
            }

        markers = _parse_final_markers(terminal["last_agent_message"])
        if markers is None:
            return {"status": "final_marker_invalid", "thread_id": thread_id}
        pipeline_id, step_done = markers
        return {
            "status": "terminal",
            "thread_id": thread_id,
            "pipeline_id": pipeline_id,
            "step_done": step_done,
            "timestamp": terminal["timestamp"],
            "turn_id": terminal["turn_id"],
            "completed_at": terminal["completed_at"],
            "rollout_file": path.name,
        }


def _parse_final_markers(message: str) -> tuple[str, int] | None:
    pipeline_ids = _PIPELINE_MARKER_RE.findall(message)
    steps = _STEP_MARKER_RE.findall(message)
    if len(pipeline_ids) != 1 or len(steps) != 1:
        return None
    return pipeline_ids[0], int(steps[0])


def _fingerprint_file(path: Path, root: Path) -> dict[str, Any] | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        before = resolved.stat()
        if not resolved.is_file() or before.st_size <= 0:
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = resolved.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return {
            "path": resolved.relative_to(root).as_posix(),
            "sha256": digest.hexdigest(),
            "size": after.st_size,
        }
    except (OSError, ValueError):
        return None


def _valid_paper_id(paper_id: str) -> bool:
    return bool(_PAPER_ID_RE.fullmatch(str(paper_id or ""))) and paper_id not in {".", ".."}
