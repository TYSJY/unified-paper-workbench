from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.audit_publish_boundary import (
    audit_paths,
    customer_bundle_paths,
    tracked_paths,
)
from tools.inject_commercial_public_key import inject_public_key
from unified_paper_orchestrator.commercial.contracts import (
    ENTITLEMENTS_SCHEMA,
    ENVELOPE_SCHEMA,
    LICENSE_SCHEMA,
    RELEASE_SCHEMA,
)
from unified_paper_orchestrator.commercial.crypto import (
    SignatureError,
    generate_private_key,
    private_key_to_pem,
    public_key_id,
    public_key_to_pem,
)
from unified_paper_orchestrator.commercial.diagnostics import DIAGNOSTIC_SCHEMA
from unified_paper_orchestrator.commercial.runtime import TRUSTED_KEYS_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


def test_commercial_json_schemas_match_executable_contract_versions() -> None:
    expected = {
        "signed-envelope.schema.json": ENVELOPE_SCHEMA,
        "entitlements.schema.json": ENTITLEMENTS_SCHEMA,
        "license.schema.json": LICENSE_SCHEMA,
        "release.schema.json": RELEASE_SCHEMA,
        "diagnostic.schema.json": DIAGNOSTIC_SCHEMA,
    }
    for filename, schema_name in expected.items():
        document = json.loads(
            (ROOT / "contracts" / "commercial" / filename).read_text(encoding="utf-8")
        )
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["type"] == "object"
        assert document["additionalProperties"] is False
        assert document["properties"]["schema"]["const"] == schema_name


def test_customer_release_profile_uses_the_production_https_control_plane() -> None:
    customer = json.loads(
        (ROOT / "release_profiles" / "customer-closed-beta.json").read_text(
            encoding="utf-8"
        )
    )
    internal = json.loads(
        (ROOT / "release_profiles" / "internal.json").read_text(encoding="utf-8")
    )
    assert customer["control_plane_url"] == (
        "https://unified-paper-workbench.gptpro20x836074.chatgpt.site/"
        "api/control-plane"
    )
    assert customer["commercial_enforcement"] is True
    assert customer["allow_runtime_configuration"] is False
    assert internal["control_plane_url"] == "http://127.0.0.1:8765"
    assert internal["commercial_enforcement"] is False


def test_publish_boundary_rejects_state_private_keys_and_personal_fixtures(
    tmp_path: Path,
) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "runtime.sqlite3").write_bytes(b"")
    (tmp_path / "private.txt").write_text(
        "-----BEGIN " + "PRIVATE KEY-----\nopaque\n",
        encoding="utf-8",
    )
    (tmp_path / "fixture.md").write_text(
        "Name: " + "Zhichao" + " Zhan",
        encoding="utf-8",
    )

    findings = audit_paths(
        tmp_path,
        [
            "state/runtime.sqlite3",
            "private.txt",
            "fixture.md",
        ],
        repository=False,
    )
    codes = {item.code for item in findings}
    assert "denied_path" in codes
    assert "denied_suffix" in codes
    assert "private_key" in codes
    assert "personal_fixture" in codes


def test_customer_bundle_accepts_only_release_artifacts(tmp_path: Path) -> None:
    (tmp_path / "UnifiedPaperOrchestrator.exe").write_bytes(b"MZ synthetic")
    (tmp_path / "LICENSE.txt").write_text("preview terms", encoding="utf-8")
    paths = customer_bundle_paths(tmp_path)
    assert audit_paths(tmp_path, paths, repository=False) == []

    (tmp_path / "server.py").write_text("print('not for customers')", encoding="utf-8")
    findings = audit_paths(
        tmp_path,
        customer_bundle_paths(tmp_path),
        repository=False,
    )
    assert any(item.code == "source_in_customer_bundle" for item in findings)


def test_public_key_injection_is_deterministic_and_rejects_private_key(
    tmp_path: Path,
) -> None:
    private_key = generate_private_key()
    public_path = tmp_path / "release.public.pem"
    public_path.write_bytes(public_key_to_pem(private_key.public_key()))
    keyring_path = tmp_path / "trusted_keys.json"

    first = inject_public_key(public_path, keyring_path)
    second = inject_public_key(public_path, keyring_path)
    assert first == second
    assert first["schema"] == TRUSTED_KEYS_SCHEMA
    assert first["keys"][0]["key_id"] == public_key_id(private_key.public_key())
    assert "PRIVATE KEY" not in keyring_path.read_text(encoding="utf-8")

    private_path = tmp_path / "release.private.pem"
    private_path.write_bytes(private_key_to_pem(private_key))
    with pytest.raises(SignatureError):
        inject_public_key(private_path, keyring_path)


def test_current_tracked_repository_passes_publish_boundary() -> None:
    paths = tracked_paths(ROOT)
    assert paths
    assert audit_paths(ROOT, paths, repository=True) == []
