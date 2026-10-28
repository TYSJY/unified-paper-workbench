"""Find the selected paper's visible outputs without choosing individual figures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


STAGES = ("journal_author", "figures", "writing", "submission")


def _paper_dir(root: Path, paper_id: str) -> Path | None:
    papers = (root / "papers").resolve()
    target = (papers / paper_id).resolve()
    return target if target.parent == papers and paper_id not in {"", ".", ".."} else None


def _inside(path: Path, paper: Path) -> bool:
    return path.resolve().is_relative_to(paper)


def _stage_order(page: str, *, pdf: bool = False) -> tuple[str, ...]:
    # Writing is an intermediate source view; the PDF shortcut opens the most
    # advanced available manuscript unless a figure/journal page was selected.
    first = page if page in STAGES and (not pdf or page in {"figures", "journal_author"}) else ""
    return tuple(dict.fromkeys(([first] if first else []) + list(STAGES)))


def _directories(root: Path, paper: Path, stage: str, record: Mapping[str, Any]) -> list[Path]:
    preferred = {"figures": ("delivery", "submit"), "journal_author": ("package",)}.get(stage, ())
    candidates = [paper / name for name in preferred]
    stages = record.get("stages", {})
    value = stages.get(stage, {}) if isinstance(stages, Mapping) else {}
    artifact = str(value.get("artifact") or "") if isinstance(value, Mapping) else ""
    if artifact:
        path = Path(artifact)
        path = path if path.is_absolute() else root / path
        if _inside(path, paper) and path.exists():
            candidates.append(path if path.is_dir() else path.parent)
    candidates.extend([paper, paper / "work", paper / "source_submit"])
    return list(dict.fromkeys(path for path in candidates if path.is_dir() and _inside(path, paper)))


def paper_output_directory(roots: Mapping[str, Path], paper_id: str, page: str,
                           record: Mapping[str, Any]) -> Path | None:
    for stage in _stage_order(page):
        root = Path(roots[stage])
        paper = _paper_dir(root, paper_id)
        if paper is not None:
            directories = _directories(root, paper, stage, record)
            if directories:
                return directories[0]
    return None


def paper_pdf(roots: Mapping[str, Path], paper_id: str, page: str,
              record: Mapping[str, Any]) -> Path | None:
    for stage in _stage_order(page, pdf=True):
        root = Path(roots[stage]).resolve()
        paper = _paper_dir(root, paper_id)
        if paper is None:
            continue
        for directory in _directories(root, paper, stage, record):
            candidates: list[Path] = []
            try:
                manifest = json.loads((directory / "submission_manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            if isinstance(manifest, dict):
                uploads = manifest.get("upload_candidates", {})
                values = [manifest.get("compiled_pdf")]
                if isinstance(uploads, dict):
                    values.insert(0, uploads.get("manuscript_review_pdf"))
                if manifest.get("submit_tex"):
                    values.append(str(Path(str(manifest["submit_tex"])).with_suffix(".pdf")))
                for value in values:
                    if isinstance(value, str) and value:
                        candidates.extend([directory / value, root / value])
            candidates.extend([directory / "main.pdf", directory / f"{paper_id}.pdf", directory / "paper.pdf"])
            for path in candidates:
                if path.suffix.lower() != ".pdf" or not _inside(path, paper):
                    continue
                try:
                    with path.open("rb") as stream:
                        if stream.read(5) == b"%PDF-":
                            return path.resolve()
                except OSError:
                    pass
    return None
