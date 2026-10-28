from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


FIGURE_PROMPT_FILES: Mapping[int, str] = {
    1: "01_research_reference_board.md",
    2: "02_make_figure_plan.md",
    3: "03_generate_figures_and_submit_package.md",
    4: "04_single_figure_revision.md",
}

# These hashes bind the V2 dispatcher to the already tuned prompt files.  The
# dispatcher reads those files in place; it never rewrites or copies them.
FIGURE_PROMPT_SHA256: Mapping[int, str] = {
    1: "DB57ABB87F3A483E59B45771B3F368153543A23C954D3CBD04F5782DFA46D98A",
    2: "6EBECCC8C82F847C47DA8831CDA8E3F6D2645EA3B9F65B74239749DA1F17890C",
    3: "06C54D54571A4BE6EB40195172E0EE34ABB99CC1181E511AE765DA8393EB80CF",
    4: "6795E113A7D9B52D6F4A6CEDD975826F198A91D6139C4F5B45C968B408AC79FF",
}


class FigurePromptIntegrityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FigurePromptFingerprint:
    step: int
    path: Path
    sha256: str
    size: int


def prompt_path(root: Path, step: int) -> Path:
    try:
        filename = FIGURE_PROMPT_FILES[int(step)]
    except (KeyError, TypeError, ValueError) as exc:
        raise FigurePromptIntegrityError(
            "FIGURE_PROMPT_STEP_INVALID",
            f"unsupported figure prompt step: {step!r}",
        ) from exc
    return Path(root) / "prompts" / filename


def fingerprint_figure_prompt(root: Path, step: int) -> FigurePromptFingerprint:
    path = prompt_path(root, step)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise FigurePromptIntegrityError(
            "FIGURE_PROMPT_MISSING",
            f"cannot read tuned figure prompt for Step {step}: {path}",
        ) from exc
    return FigurePromptFingerprint(
        step=int(step),
        path=path,
        sha256=hashlib.sha256(payload).hexdigest().upper(),
        size=len(payload),
    )


def validate_figure_prompt(root: Path, step: int) -> FigurePromptFingerprint:
    fingerprint = fingerprint_figure_prompt(root, step)
    expected = FIGURE_PROMPT_SHA256[fingerprint.step]
    if fingerprint.sha256 != expected:
        raise FigurePromptIntegrityError(
            "FIGURE_PROMPT_HASH_MISMATCH",
            (
                f"figure Step {step} prompt bytes changed; expected {expected}, "
                f"observed {fingerprint.sha256}"
            ),
        )
    return fingerprint


def validate_all_figure_prompts(root: Path) -> dict[int, FigurePromptFingerprint]:
    return {step: validate_figure_prompt(root, step) for step in sorted(FIGURE_PROMPT_FILES)}


def build_figure_step_prompt(root: Path, paper_id: str, source_tex: str, step: int, *, start_step: int) -> str:
    """Build the legacy-compatible outer command without changing prompt text."""

    fingerprint = validate_figure_prompt(root, step)
    source = str(source_tex or f"papers/{paper_id}/input.tex").replace("\\", "/").lstrip("/")
    command = (
        f"Enter `paper-figure-codex`, then run prompts/{fingerprint.path.name} "
        f"for {source}"
    )
    if step == 2 and int(start_step) >= 2 and not (Path(root) / "papers" / paper_id / "reference_board.md").exists():
        command += (
            "\n\nStep 01 was intentionally skipped by the unified controller. Reuse the closest\n"
            "existing shared reference pool under refs/ for this local-LLM systems paper.\n"
            "If no close pool exists, continue from input.tex only and record that no local\n"
            "reference screenshots were used. Do not download a new reference board in this\n"
            "Step 02 run."
        )
    return command


__all__ = [
    "FIGURE_PROMPT_FILES",
    "FIGURE_PROMPT_SHA256",
    "FigurePromptFingerprint",
    "FigurePromptIntegrityError",
    "build_figure_step_prompt",
    "fingerprint_figure_prompt",
    "prompt_path",
    "validate_all_figure_prompts",
    "validate_figure_prompt",
]
