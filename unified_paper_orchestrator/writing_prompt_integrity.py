from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


TUNED_WRITING_PROMPT_FILES: dict[int, str] = {
    1: "01_find_one_idea.md",
    2: "02_collect_references.md",
    3: "03_write_draft_tex.md",
    4: "04_upgrade_submission_draft.md",
    5: "05_complete_figures_tables_results.md",
    6: "06_clean_verified_submission_draft.md",
    7: "07_review_round_1.md",
    8: "08_revise_after_review_round_1.md",
    9: "09_submission_maturity_cleanup.md",
    10: "10_final_submission_hardening.md",
}

# Code-owned production seal copied from tests/contracts/v1_internal_prompts.json.
# Keep this independent of the live files: deriving the expected values from disk
# would silently accept a user-tuned prompt that changed between writing steps.
TUNED_WRITING_PROMPT_SHA256: Mapping[int, str] = MappingProxyType(
    {
        1: "f0d79ecc6ffc68f19a5a7dd31aa7158991c9b751d577ce69cd3adfb948c95fa6",
        2: "4c7ec9e5c91aa4f8b8562316886bd6aaabeded08d644f5dcd940e3096f2176ed",
        3: "3df365c23ebc2640b792c64ed7421103caa2cb1d2ec9ad03be1d75e9e3d67601",
        4: "13e2a8d526ef0bd07bb46d938934a0042b09c76c59d17b293fa6c571a4d1f070",
        5: "f50231764c097ff7bcaf7c5c3e9951eea57a3300d866673f4ed8047d8568237d",
        6: "462901ff9b906737ac4650156d45d1afedba7817dc690232a6d1ecb7ca827e1e",
        7: "ba1cc676dc414fdef6ab8b16c49a5a138d4dd6052af3a6be5faed0379ebdf687",
        8: "e2e29f6748046fe76f210a8cf779dcc95c8290743dbbb94551db57071863ca88",
        9: "c40058f4df7aebc845c7cfedc55f5c5efbe52552f88f5322463e06477d4ff35c",
        10: "de10dcb1b877df84c5b905a2c5a543b4ec80656d0aa6412666402b73df87f852",
    }
)

_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class WritingPromptIntegrityError(RuntimeError):
    pass


def _is_reparse(path: Path, value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    if int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE:
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(callable(is_junction) and is_junction(path))


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_mode),
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", round(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_file_attributes", 0)),
    )


def writing_prompt_fingerprint(root: Path | str, step: int) -> dict[str, Any]:
    """Read one tuned prompt once and return a stable, contained byte fingerprint."""

    filename = TUNED_WRITING_PROMPT_FILES.get(int(step))
    if filename is None:
        raise WritingPromptIntegrityError(f"unsupported writing step: {step}")
    root_path = Path(root).absolute()
    path = (root_path / "prompts" / filename).absolute()
    try:
        path.relative_to(root_path)
    except ValueError as exc:
        raise WritingPromptIntegrityError("writing prompt path escapes the project root") from exc

    cursor = root_path
    try:
        for part in path.relative_to(root_path).parts:
            cursor /= part
            value = os.lstat(cursor)
            if _is_reparse(cursor, value):
                raise WritingPromptIntegrityError(f"writing prompt path uses a reparse component: {cursor}")
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise WritingPromptIntegrityError("writing prompt must be a non-empty regular file")
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            data = handle.read()
            opened_after = os.fstat(handle.fileno())
        after = os.lstat(path)
    except WritingPromptIntegrityError:
        raise
    except OSError as exc:
        raise WritingPromptIntegrityError(f"cannot read tuned writing prompt: {type(exc).__name__}") from exc
    signature = _signature(before)
    if (
        signature != _signature(opened_before)
        or signature != _signature(opened_after)
        or signature != _signature(after)
        or len(data) != before.st_size
    ):
        raise WritingPromptIntegrityError("tuned writing prompt changed while it was being read")
    return {
        "step": int(step),
        "path": path.relative_to(root_path).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "stat_signature": list(signature),
    }


def writing_prompt_unchanged(root: Path | str, expected: dict[str, Any]) -> bool:
    try:
        current = writing_prompt_fingerprint(root, int(expected.get("step") or 0))
    except (TypeError, ValueError, WritingPromptIntegrityError):
        return False
    return all(current.get(key) == expected.get(key) for key in ("step", "path", "sha256", "size", "stat_signature"))


__all__ = [
    "TUNED_WRITING_PROMPT_FILES",
    "TUNED_WRITING_PROMPT_SHA256",
    "WritingPromptIntegrityError",
    "writing_prompt_fingerprint",
    "writing_prompt_unchanged",
]
