"""Inject a reviewed Ed25519 public key into the desktop trust keyring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from unified_paper_orchestrator.commercial.crypto import (
    load_public_key,
    public_key_id,
    public_key_to_pem,
)
from unified_paper_orchestrator.commercial.runtime import TRUSTED_KEYS_SCHEMA
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic


def inject_public_key(
    public_key_path: Path,
    output_path: Path,
    *,
    replace: bool = False,
    expected_key_id: str = "",
) -> dict[str, Any]:
    key = load_public_key(public_key_path)
    key_id = public_key_id(key)
    if expected_key_id and key_id != expected_key_id:
        raise ValueError(
            f"Public key ID mismatch: expected {expected_key_id}, observed {key_id}."
        )
    if replace or not output_path.exists():
        records: list[dict[str, str]] = []
    else:
        current = read_json(output_path)
        if (
            not isinstance(current, dict)
            or current.get("schema") != TRUSTED_KEYS_SCHEMA
            or not isinstance(current.get("keys"), list)
        ):
            raise ValueError("Existing trusted-key file is invalid.")
        records = [dict(item) for item in current["keys"] if isinstance(item, dict)]
    record = {
        "key_id": key_id,
        "public_key_pem": public_key_to_pem(key).decode("ascii"),
    }
    by_id = {
        str(item.get("key_id") or ""): item
        for item in records
        if set(item) == {"key_id", "public_key_pem"}
    }
    by_id[key_id] = record
    payload = {
        "schema": TRUSTED_KEYS_SCHEMA,
        "keys": [by_id[item] for item in sorted(by_id)],
    }
    write_json_atomic(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject an Ed25519 public key; private keys are rejected."
    )
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("unified_paper_orchestrator/commercial/trusted_keys.json"),
    )
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--expected-key-id", default="")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    key = load_public_key(args.public_key)
    key_id = public_key_id(key)
    if args.expected_key_id and key_id != args.expected_key_id:
        parser.error(
            f"public key ID mismatch: expected {args.expected_key_id}, observed {key_id}"
        )
    if args.check_only:
        print(json.dumps({"key_id": key_id, "public_key": True}))
        return 0
    payload = inject_public_key(
        args.public_key,
        args.output,
        replace=args.replace,
        expected_key_id=args.expected_key_id,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "key_id": key_id,
                "key_count": len(payload["keys"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
