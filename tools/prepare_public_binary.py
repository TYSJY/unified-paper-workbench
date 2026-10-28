"""Prepare a public b11 payload from an explicitly supplied verified executable.

No accounts, workspace state, browser profiles, or manuscripts are copied.
Run the installer builder separately after reviewing the generated inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile

from PyInstaller.archive.readers import CArchiveReader


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream-source-dir", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    target = args.output.resolve()
    if not target.is_relative_to(repo / "build") or target.exists():
        raise ValueError("Use a fresh directory under build/.")
    expected = "ba477c09f5537fd15931eb98de002996c391962bb849631f3b57a8e8b28bcccc"
    observed = hashlib.sha256(args.exe.read_bytes()).hexdigest()
    if observed != expected:
        raise ValueError("This recipe is for the audited b11 executable only.")
    target.mkdir(parents=True)
    addon = target / "addon"
    addon.mkdir()
    shutil.copytree(repo / "workspace_templates", addon / "workspace-template")
    shutil.copytree(repo / "prompts", addon / "prompts")
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(repo / name, addon / (name + ".txt" if name == "LICENSE" else name))

    reader = CArchiveReader(str(args.exe))
    licenses = addon / "licenses"
    # Wheel metadata can omit the copyleft texts even when its license
    # expression offers LGPL/GPL. Preserve the actual texts from the exact
    # unmodified upstream source releases, not only their commercial notice.
    source_archives = sorted(args.upstream_source_dir.glob("*.tar.*"))
    if not source_archives:
        raise ValueError("Matching upstream source archives are required.")
    for source_archive in source_archives:
        with tarfile.open(source_archive) as archive:
            for member in archive.getmembers():
                if not member.isfile() or member.size > 1024 * 1024:
                    continue
                parts = Path(member.name).parts
                if not any(part.lower().startswith(("license", "licence", "copying", "copyright", "notice")) for part in parts):
                    continue
                relative = Path(*parts[1:])
                destination = licenses / "upstream" / source_archive.name / relative
                if not destination.resolve().is_relative_to(licenses.resolve()):
                    raise ValueError("Unsafe upstream notice path.")
                stream = archive.extractfile(member)
                if stream is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(stream.read())
    count = 0
    # Include notices already shipped inside the binary, made easy to find.
    for name, item in reader.toc.items():
        lower = name.lower().replace("\\", "/")
        if any(part.startswith(("license", "licence", "copying", "copyright", "notice"))
               for part in lower.split("/")) and not lower.endswith((".exe", ".dll", ".pyd")):
            destination = licenses / "bundled" / Path(name.replace("\\", "/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(reader.extract(name))
            count += 1

    # Resolve distributions actually represented in the frozen Python archive.
    for folder in (repo / "runtime/sdk-0.147.0", repo / "runtime/word-python"):
        sys.path.insert(0, str(folder))
    top_modules = {name.split(".")[0] for name in reader.open_embedded_archive("PYZ.pyz").toc}
    mapping = importlib.metadata.packages_distributions()
    distributions = {name for module in top_modules for name in mapping.get(module, [])}
    distributions.update({"PySide6-Essentials", "shiboken6", "PyInstaller", "openai-codex", "openai-codex-cli-bin"})
    inventory = []
    for name in sorted(distributions, key=str.lower):
        dist = importlib.metadata.distribution(name)
        dest = licenses / re.sub(r"[^A-Za-z0-9_.-]", "_", dist.metadata["Name"])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "METADATA.txt").write_text(dist.read_text("METADATA") or "", encoding="utf-8")
        copied = 0
        for file in dist.files or []:
            path = Path(str(file).replace("\\", "/"))
            if not any(part.lower().startswith(("license", "licence", "copying", "copyright", "notice")) for part in path.parts):
                continue
            source = Path(dist.locate_file(file))
            if not source.is_file():
                continue
            destination = dest / (str(copied) + "-" + source.name)
            shutil.copy2(source, destination)
            copied += 1
        inventory.append({"name": dist.metadata["Name"], "version": dist.version, "license_files": copied})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")

    (addon / "DEPENDENCIES.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    (addon / "SOURCE_AND_RELINKING.md").write_text("""# Source and library replacement

This preview includes Python, Qt/PySide6, Codex, Pandoc, and other open-source
components. Project-owned source is GNU AGPL-3.0-only. Dependencies keep their
own licenses; readable notices are in licenses/ and inside the executable.

Application and installer source (the release tag identifies the exact source):
https://github.com/TYSJY/unified-paper-workbench/tree/v0.12.0b11-preview

Matching Qt/PySide6, Pandoc and Codex source archives are provided alongside the
installer as THIRD_PARTY_SOURCES.zip on the same GitHub Release. Its manifest
records upstream URLs and SHA-256 checksums. The bundled libraries are unmodified.

Users may modify, replace, debug, and rebuild the application with compatible
library versions. There is no contractual restriction on reverse engineering
for debugging such changes. The executable uses PyInstaller one-file extraction
and shared Qt DLLs. Rebuild from the tagged source with the chosen Qt/PySide6
environment and tools/build_exe.ps1 (or equivalent PyInstaller arguments shown
there), then package the new executable using tools/build_customer_installer.ps1.
Adjust the development-tool path pins for your own verified toolchain.

Microsoft Word, browsers, model accounts, and LaTeX are external prerequisites,
not redistributed products. No model credit or account is included.
""", encoding="utf-8")
    files = []
    for path in sorted(addon.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files.append({"path": path.relative_to(addon).as_posix(), "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    report = {"core_sha256": observed, "core_bytes": args.exe.stat().st_size,
        "bundled_notice_files": count, "distributions": inventory, "files": files,
        "account_records": 0, "manuscripts": 0}
    (target / "inventory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "notice_files": count, "distributions": len(inventory), "payload_files": len(files)}))


if __name__ == "__main__":
    main()
