from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Iterable


CHUNK_SIZE = 1024 * 1024


def _long_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_long_path(path), "rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _iter_files(
    root: Path,
    *,
    excluded_dirs: set[str],
    excluded_globs: tuple[str, ...],
) -> Iterable[Path]:
    root = root.resolve()
    for current, directories, files in os.walk(_long_path(root)):
        directories[:] = [name for name in directories if name not in excluded_dirs]
        current_path = Path(current)
        for name in files:
            if any(fnmatch.fnmatch(name, pattern) for pattern in excluded_globs):
                continue
            yield current_path / name


def _relative(path: Path, root: Path) -> str:
    root_text = _long_path(root).rstrip("\\/")
    path_text = str(path)
    prefix = root_text + os.sep
    if not path_text.lower().startswith(prefix.lower()):
        raise ValueError(f"path is outside root: {path}")
    return path_text[len(prefix) :].replace("\\", "/")


def _inventory(
    root: Path,
    *,
    excluded_dirs: set[str],
    excluded_globs: tuple[str, ...],
    skip_relatives: set[str] | None = None,
) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    skipped = skip_relatives or set()
    for path in _iter_files(root, excluded_dirs=excluded_dirs, excluded_globs=excluded_globs):
        relative = _relative(path, root)
        if relative in skipped:
            continue
        stat = os.stat(_long_path(path))
        result[relative] = (stat.st_size, _hash_file(path))
    return result


def create_manifest(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output_relative = None
    try:
        output_relative = output.relative_to(root).as_posix()
    except ValueError:
        pass
    skipped = {output_relative} if output_relative else set()
    inventory = _inventory(
        root,
        excluded_dirs=set(args.exclude_dir),
        excluded_globs=tuple(args.exclude_glob),
        skip_relatives=skipped,
    )
    with open(_long_path(output), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("RelativePath", "Length", "SHA256"))
        for relative in sorted(inventory):
            length, digest = inventory[relative]
            writer.writerow((relative, length, digest))
    print(f"files={len(inventory)}")
    print(f"bytes={sum(length for length, _digest in inventory.values())}")
    print(f"manifest_sha256={_hash_file(output)}")
    return 0


def verify_manifest(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    manifest = Path(args.manifest).resolve()
    failures: list[str] = []
    checked = 0
    with open(_long_path(manifest), "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            checked += 1
            relative = str(row["RelativePath"])
            path = root / Path(relative.replace("/", os.sep))
            try:
                stat = os.stat(_long_path(path))
            except FileNotFoundError:
                failures.append(f"missing:{relative}")
                continue
            expected_length = int(row["Length"])
            if stat.st_size != expected_length:
                failures.append(f"length:{relative}:{stat.st_size}!={expected_length}")
                continue
            actual = _hash_file(path)
            expected = str(row["SHA256"]).upper()
            if actual != expected:
                failures.append(f"sha256:{relative}:{actual}!={expected}")
    print(f"checked={checked}")
    print(f"failures={len(failures)}")
    for failure in failures[:100]:
        print(failure)
    return 1 if failures else 0


def compare_trees(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    destination = Path(args.destination).resolve()
    options = {
        "excluded_dirs": set(args.exclude_dir),
        "excluded_globs": tuple(args.exclude_glob),
    }
    source_inventory = _inventory(source, **options)
    destination_inventory = _inventory(destination, **options)
    missing = sorted(set(source_inventory) - set(destination_inventory))
    extra = sorted(set(destination_inventory) - set(source_inventory))
    changed = sorted(
        relative
        for relative in set(source_inventory) & set(destination_inventory)
        if source_inventory[relative] != destination_inventory[relative]
    )
    print(f"source_files={len(source_inventory)}")
    print(f"destination_files={len(destination_inventory)}")
    print(f"missing={len(missing)}")
    print(f"extra={len(extra)}")
    print(f"changed={len(changed)}")
    for category, values in (("missing", missing), ("extra", extra), ("changed", changed)):
        for relative in values[:100]:
            print(f"{category}:{relative}")
    return 1 if missing or extra or changed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Long-path-safe V1 backup verification")
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--root", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--exclude-dir", action="append", default=[])
    manifest.add_argument("--exclude-glob", action="append", default=[])
    manifest.set_defaults(func=create_manifest)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.set_defaults(func=verify_manifest)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--source", required=True)
    compare.add_argument("--destination", required=True)
    compare.add_argument("--exclude-dir", action="append", default=[])
    compare.add_argument("--exclude-glob", action="append", default=[])
    compare.set_defaults(func=compare_trees)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
