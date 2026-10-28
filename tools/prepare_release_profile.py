"""Validate and stage immutable commercial assets for one desktop build."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tools.inject_commercial_public_key import inject_public_key
from unified_paper_orchestrator.commercial.distribution import (
    DistributionPolicy,
    load_distribution_policy,
)
from unified_paper_orchestrator.commercial.runtime import TRUSTED_KEYS_SCHEMA
from unified_paper_orchestrator.io_utils import read_json, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
SOURCE_TRUSTED_KEYS = (
    ROOT / "unified_paper_orchestrator" / "commercial" / "trusted_keys.json"
)


def _validate_internal_keyring(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema") != TRUSTED_KEYS_SCHEMA
        or set(value) != {"schema", "keys"}
        or not isinstance(value.get("keys"), list)
    ):
        raise ValueError("The internal trusted-key source is invalid.")
    records: list[dict[str, str]] = []
    for item in value["keys"]:
        if not isinstance(item, dict) or set(item) != {
            "key_id",
            "public_key_pem",
        }:
            raise ValueError("The internal trusted-key source has an invalid record.")
        records.append(
            {
                "key_id": str(item["key_id"] or "").strip(),
                "public_key_pem": str(item["public_key_pem"] or "").strip(),
            }
        )
    return {"schema": TRUSTED_KEYS_SCHEMA, "keys": records}


def prepare_release_profile(
    profile_path: Path,
    output_dir: Path,
    *,
    public_key_path: Path | None = None,
) -> dict[str, Any]:
    """Stage a canonical policy and public-only keyring for PyInstaller."""

    policy: DistributionPolicy = load_distribution_policy(profile_path)
    if policy.customer_edition and public_key_path is None:
        raise ValueError("Customer builds require an Ed25519 public signing key.")
    if not policy.customer_edition and public_key_path is not None:
        raise ValueError("A commercial public key is only accepted for customer builds.")

    output_dir.mkdir(parents=True, exist_ok=True)
    policy_output = output_dir / "distribution_policy.json"
    keyring_output = output_dir / "trusted_keys.json"
    write_json_atomic(policy_output, policy.to_dict())

    if policy.customer_edition:
        assert public_key_path is not None
        keyring = inject_public_key(
            public_key_path,
            keyring_output,
            replace=True,
        )
    else:
        keyring = _validate_internal_keyring(read_json(SOURCE_TRUSTED_KEYS))
        write_json_atomic(keyring_output, keyring)

    return {
        "edition": policy.edition,
        "commercial_enforcement": policy.commercial_enforcement,
        "release_channel": policy.release_channel,
        "trusted_public_key_count": len(keyring["keys"]),
        "distribution_policy": str(policy_output),
        "trusted_keys": str(keyring_output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage immutable distribution policy and public-only trust keys."
    )
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--public-key", type=Path)
    args = parser.parse_args()
    result = prepare_release_profile(
        args.profile,
        args.output_dir,
        public_key_path=args.public_key,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
