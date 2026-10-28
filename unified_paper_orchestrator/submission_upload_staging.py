"""Deterministic upload-package staging for journal submission systems."""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


_GRAPHICS_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".eps")
_INCLUDE_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}")
_BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\s*\{([^{}]+)\}")


class SubmissionUploadStagingError(ValueError):
    """Raised when a safe, complete source archive cannot be staged."""


@dataclass(frozen=True)
class StagedUploadArchive:
    path: Path
    sha256: str
    files: tuple[str, ...]
    bytes: int


def _dependency_path(
    package_dir: Path,
    owner: Path,
    raw_value: str,
    suffixes: tuple[str, ...],
) -> Path | None:
    value = str(raw_value or "").strip().replace("\\", "/")
    if not value or "\\" in value or value.startswith("/"):
        return None
    candidate = owner.parent / value
    choices = (candidate,) if candidate.suffix else tuple(
        candidate.with_suffix(suffix) for suffix in suffixes
    )
    root = package_dir.resolve()
    for choice in choices:
        resolved = choice.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    return None


def _latex_source_files(package_dir: Path) -> tuple[Path, ...]:
    root = package_dir.resolve()
    main_tex = root / "main.tex"
    if not main_tex.is_file() or main_tex.is_symlink():
        raise SubmissionUploadStagingError(
            "submission package is missing a regular main.tex"
        )

    selected: set[Path] = {main_tex}
    pending = [main_tex]
    missing: list[str] = []
    while pending:
        tex_path = pending.pop()
        text = tex_path.read_text(encoding="utf-8", errors="replace")
        for raw in _INCLUDE_RE.findall(text):
            dependency = _dependency_path(root, tex_path, raw, (".tex",))
            if dependency is None:
                missing.append(f"{tex_path.name}: {raw}")
            elif dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
        for raw in _GRAPHICS_RE.findall(text):
            dependency = _dependency_path(root, tex_path, raw, _GRAPHICS_SUFFIXES)
            if dependency is None:
                missing.append(f"{tex_path.name}: {raw}")
            else:
                selected.add(dependency)
        for group in _BIBLIOGRAPHY_RE.findall(text):
            for raw in group.split(","):
                dependency = _dependency_path(root, tex_path, raw, (".bib",))
                if dependency is None:
                    missing.append(f"{tex_path.name}: {raw.strip()}")
                else:
                    selected.add(dependency)

    for suffix in (".cls", ".sty", ".bst", ".bbx", ".cbx", ".lbx"):
        for path in root.rglob(f"*{suffix}"):
            if path.is_file() and not path.is_symlink():
                selected.add(path.resolve())

    if missing:
        raise SubmissionUploadStagingError(
            "submission package has unresolved LaTeX dependencies: "
            + "; ".join(sorted(set(missing))[:20])
        )
    return tuple(
        sorted(
            selected,
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
    )


def stage_snapp_latex_archive(
    package_dir: Path | str,
    destination: Path | str,
) -> StagedUploadArchive:
    """Create a reproducible SNAPP LaTeX source archive from referenced files."""

    root = Path(package_dir).expanduser().resolve()
    target = Path(destination).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise SubmissionUploadStagingError(
            "submission package directory does not exist"
        )
    files = _latex_source_files(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                name = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(
                    info,
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

    payload = target.read_bytes()
    return StagedUploadArchive(
        path=target,
        sha256=hashlib.sha256(payload).hexdigest(),
        files=tuple(path.relative_to(root).as_posix() for path in files),
        bytes=len(payload),
    )
