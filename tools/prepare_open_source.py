"""Export an audited source snapshot without runtime data or Git history."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

from audit_publish_boundary import ALLOWED_REPOSITORY_ROOTS, audit_paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_relative_to(root / "build") or output.exists():
        raise ValueError("Use a new directory under build/.")
    raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)
    paths = sorted({p.decode("utf-8") for p in raw.split(b"\0") if p})
    selected = [p for p in paths if PurePosixPath(p).parts[0] in ALLOWED_REPOSITORY_ROOTS
                and (root / p).is_file()]
    findings = audit_paths(root, selected, repository=True)
    output.mkdir(parents=True)
    report = {"ok": not findings, "files": len(selected), "git_history_included": False,
              "runtime_data_included": False, "findings": [asdict(x) for x in findings]}
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if findings:
        print(json.dumps({"ok": False, "finding_count": len(findings), "report": str(output / "audit.json")}))
        return 1
    target = output / "source"
    manifest = []
    with zipfile.ZipFile(output / "unified-paper-workbench-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in selected:
            source = root / relative
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            data = destination.read_bytes()
            manifest.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            archive.write(destination, relative)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "files": len(selected), "archive": str(output / "unified-paper-workbench-source.zip")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
