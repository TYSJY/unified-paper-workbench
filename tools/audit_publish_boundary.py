"""Fail closed when a source or customer bundle crosses the release boundary."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


MAX_SCAN_BYTES = 8 * 1024 * 1024
ALLOWED_REPOSITORY_ROOTS = {
    ".github",
    ".gitignore",
    "LICENSE",
    "MANIFEST.in",
    "PRIVACY.md",
    "README.md",
    "README.en.md",
    "CONTRIBUTING.md",
    "NOTICE",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "chatgpt_web_drawing_prompts",
    "chatgpt_web_prompts",
    "commercial_control_plane",
    "contracts",
    "docs",
    "prompts",
    "pyproject.toml",
    "release_profiles",
    "single_file_handoff_prompts",
    "tests",
    "tools",
    "unified_paper_orchestrator",
    "unified_paper_orchestrator_v2",
    "workspace_templates",
}
DENIED_PARTS = {
    ".commercial",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "browser_profiles",
    "build",
    "dist",
    "dist_v2",
    "page_snapshots",
    "releases",
    "screenshots",
    "state",
    "tmp",
    "user data",
}
DENIED_NAMES = {
    "api_token.local",
    "credential_store.local.json",
    "development-signing-key.pem",
}
DENIED_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".env",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
CUSTOMER_ALLOWED_SUFFIXES = {
    ".exe",
    ".json",
    ".md",
    ".sha256",
    ".sig",
    ".txt",
}
CUSTOMER_DENIED_SOURCE_SUFFIXES = {
    ".c",
    ".cpp",
    ".h",
    ".js",
    ".py",
    ".pyc",
    ".ps1",
    ".qml",
    ".ts",
}
CONTENT_PATTERNS = {
    "private_key": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "openai_key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
}
PERSONAL_MARKERS = (
    b"zhanzhi" + b"chao",
    b"Zhichao" + b" Zhan",
    b"Xiamen University" + b" Tan Kah Kee College",
)
SYNTHETIC_SECRET_MARKERS = (
    b"corrupt",
    b"example",
    b"exception",
    b"must-never",
    b"must-",
    b"private",
    b"sentinel",
    b"synthetic",
    b"test",
)


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    detail: str


def _normalise_relative(value: str | Path) -> PurePosixPath:
    text = str(value).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text)


def tracked_paths(root: Path) -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [
        _normalise_relative(item.decode("utf-8"))
        for item in result.stdout.split(b"\0")
        if item
    ]


def _path_findings(path: PurePosixPath, *, repository: bool) -> list[Finding]:
    findings: list[Finding] = []
    display = path.as_posix()
    folded_parts = {part.casefold() for part in path.parts}
    denied_parts = sorted(folded_parts & DENIED_PARTS)
    if denied_parts:
        findings.append(
            Finding("denied_path", display, f"denied path component: {denied_parts[0]}")
        )
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name in DENIED_NAMES:
        findings.append(Finding("denied_file", display, "local secret/state filename"))
    if suffix in DENIED_SUFFIXES and name != ".env.example":
        findings.append(Finding("denied_suffix", display, suffix))
    if repository:
        root_name = path.parts[0] if path.parts else ""
        if root_name not in ALLOWED_REPOSITORY_ROOTS:
            findings.append(
                Finding("unknown_repository_root", display, root_name or "<empty>")
            )
    else:
        if suffix in CUSTOMER_DENIED_SOURCE_SUFFIXES:
            findings.append(
                Finding("source_in_customer_bundle", display, suffix)
            )
        if suffix not in CUSTOMER_ALLOWED_SUFFIXES:
            findings.append(
                Finding("unexpected_customer_artifact", display, suffix or "<none>")
            )
        if suffix == ".exe" and path.name != "UnifiedPaperOrchestrator.exe":
            findings.append(
                Finding("unexpected_executable", display, path.name)
            )
    return findings


def _content_findings(root: Path, path: PurePosixPath) -> list[Finding]:
    target = root.joinpath(*path.parts)
    try:
        if not target.is_file() or target.stat().st_size > MAX_SCAN_BYTES:
            return []
        content = target.read_bytes()
    except OSError as exc:
        return [Finding("unreadable", path.as_posix(), exc.__class__.__name__)]
    findings: list[Finding] = []
    for code, pattern in CONTENT_PATTERNS.items():
        matches = list(pattern.finditer(content))
        if code == "openai_key":
            matches = [
                match
                for match in matches
                if not any(
                    marker in match.group(0).lower()
                    for marker in SYNTHETIC_SECRET_MARKERS
                )
            ]
        if matches:
            findings.append(
                Finding(code, path.as_posix(), "high-confidence sensitive content")
            )
    folded = content.lower()
    if any(marker.lower() in folded for marker in PERSONAL_MARKERS):
        findings.append(
            Finding(
                "personal_fixture",
                path.as_posix(),
                "high-confidence sensitive content",
            )
        )
    return findings


def audit_paths(
    root: Path,
    paths: Iterable[str | Path | PurePosixPath],
    *,
    repository: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    for raw in paths:
        path = _normalise_relative(raw)
        findings.extend(_path_findings(path, repository=repository))
        findings.extend(_content_findings(root, path))
    return sorted(findings, key=lambda item: (item.path, item.code, item.detail))


def customer_bundle_paths(root: Path) -> list[PurePosixPath]:
    return [
        _normalise_relative(path.relative_to(root))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit tracked source or a staged customer bundle."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--customer-bundle", type=Path)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    if args.customer_bundle:
        root = args.customer_bundle.resolve()
        paths = customer_bundle_paths(root)
        repository = False
        if not paths:
            findings = [Finding("empty_bundle", ".", "customer bundle is empty")]
        else:
            findings = audit_paths(root, paths, repository=False)
    else:
        root = args.root.resolve()
        paths = tracked_paths(root)
        repository = True
        findings = audit_paths(root, paths, repository=True)

    result = {
        "ok": not findings,
        "mode": "repository" if repository else "customer_bundle",
        "root": str(root),
        "files": len(paths),
        "findings": [asdict(item) for item in findings],
    }
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif findings:
        for item in findings:
            print(f"{item.code}: {item.path}: {item.detail}")
        print(f"Publish boundary failed with {len(findings)} finding(s).")
    else:
        print(f"Publish boundary passed: {len(paths)} file(s), mode={result['mode']}.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
