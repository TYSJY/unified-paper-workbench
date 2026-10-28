from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unified_paper_orchestrator import __version__  # noqa: E402
from unified_paper_orchestrator.config import WRITING_ROOT  # noqa: E402
from unified_paper_orchestrator_v2.contracts import canonical_json  # noqa: E402
from unified_paper_orchestrator_v2.review_canary import (  # noqa: E402
    CODEX_CLI_RELAY_REVIEW_ACTION,
    CODEX_CLI_RELAY_REVIEW_PAYLOAD_KEYS,
    REVIEW_ROUTE_EXECUTORS,
    codex_cli_relay_static_provider_payload,
    validate_review_markdown,
)


EXPECTED_VERSION = "0.4.4"
PAPER_ID = "2026-07-02_001_reconfiguration-debt-decoding"
OFFICIAL_ACTION = "writing.review_round_1.canary"
RELAY_ACTION = "writing.review_round_1.compatible_relay_canary"
CODEX_CLI_RELAY_ACTION = CODEX_CLI_RELAY_REVIEW_ACTION
OFFICIAL_EXECUTOR = "openai_review"
RELAY_EXECUTOR = "openai_compatible_relay_review"
CODEX_CLI_RELAY_EXECUTOR = REVIEW_ROUTE_EXECUTORS[CODEX_CLI_RELAY_ACTION]
OFFICIAL_ENDPOINT = "https://api.openai.com/v1"
RELAY_ENDPOINT = "https://www.qilinapi.com"
OFFICIAL_PROVIDER_KIND = "official_openai"
OFFICIAL_PROVIDER_PROFILE = "official_openai_responses_v1"
OFFICIAL_PROVIDER_ASSURANCE = "official_endpoint_contract"
RELAY_PROVIDER_KIND = "third_party_openai_compatible_relay"
RELAY_PROVIDER_PROFILE = "qilinapi_responses_v1"
RELAY_PROVIDER_ASSURANCE = "third_party_claim_unverified"
RELAY_WIRE_API = "responses"
OFFICIAL_ROUTE_KIND = "official_openai"
RELAY_ROUTE_KIND = "openai_compatible_relay"
CODEX_CLI_RELAY_ROUTE_KIND = "qilin_codex_cli_relay"
CODEX_CLI_PROVIDER_PAYLOAD = codex_cli_relay_static_provider_payload()
CODEX_CLI_PROVIDER_KIND = str(CODEX_CLI_PROVIDER_PAYLOAD["provider_kind"])
CODEX_CLI_PROVIDER_PROFILE = str(CODEX_CLI_PROVIDER_PAYLOAD["provider_profile"])
CODEX_CLI_PROVIDER_ASSURANCE = str(CODEX_CLI_PROVIDER_PAYLOAD["provider_assurance"])
CODEX_CLI_ENVELOPE_RECEIPT_KEYS = (
    "client_kind",
    "client_provenance_assurance",
    "client_config_profile",
    "reasoning_effort",
    "reasoning_effort_namespace",
    "identity_policy",
    "credential_policy",
    "billing_policy",
    "billing_account_verified",
    "chatgpt_subscription_used",
    "openai_api_account_used",
    "official_openai_endpoint_proven",
    "upstream_model_identity_verified",
    "model_identity_assurance",
    "codex_cli_sha256",
    "argv_policy_sha256",
    "environment_policy",
    "jsonl_policy",
    "output_token_limit_policy",
)
CODEX_CLI_RUNTIME_RECEIPT_KEYS = (
    "codex_cli_version",
    "billing_credential_fingerprint",
    "requested_model",
    "reported_model",
    "provider_response_id",
    "provider_external_ref_kind",
)
CODEX_THREAD_ID = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
COMMON_REVIEW_PAYLOAD_KEYS = frozenset(
    {
        "model",
        "input_roles",
        "source_prompt_sha256",
        "rendered_prompt_sha256",
        "input_manifest_sha256",
        "prompt_transform_id",
        "outer_adapter_version",
        "token_estimator_id",
        "evidence_scope",
        "staging_only",
        "max_output_tokens",
        "expected_figure_count",
        "expected_table_count",
        "expected_bibitem_count",
    }
)
RELAY_REVIEW_PAYLOAD_KEYS = frozenset(
    {
        *COMMON_REVIEW_PAYLOAD_KEYS,
        "provider_kind",
        "provider_profile",
        "provider_assurance",
        "wire_api",
        "endpoint_base_url",
        "endpoint_config_sha256",
        "third_party_data_disclosure_ack",
    }
)
KNOWN_HISTORICAL_REVIEW_SHA256 = "2819f57cba1e2392666299dbfe9d89fb1f5ba63456f23439f47b8649399c7156"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check(name: str, ok: bool, *, status: str = "failed", **evidence: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "status": "passed" if ok else status, **evidence}


def _relay_endpoint_config_sha256() -> str:
    config = {
        "provider_kind": RELAY_PROVIDER_KIND,
        "provider_profile": RELAY_PROVIDER_PROFILE,
        "provider_assurance": RELAY_PROVIDER_ASSURANCE,
        "wire_api": RELAY_WIRE_API,
        "endpoint_base_url": RELAY_ENDPOINT,
    }
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


RELAY_ENDPOINT_CONFIG_SHA256 = _relay_endpoint_config_sha256()


def _route_kind(action: str, executor: str) -> str:
    pair = (str(action or ""), str(executor or ""))
    if pair == (OFFICIAL_ACTION, OFFICIAL_EXECUTOR):
        return OFFICIAL_ROUTE_KIND
    if pair == (RELAY_ACTION, RELAY_EXECUTOR):
        return RELAY_ROUTE_KIND
    if pair == (CODEX_CLI_RELAY_ACTION, CODEX_CLI_RELAY_EXECUTOR):
        return CODEX_CLI_RELAY_ROUTE_KIND
    return ""


def _is_non_offline_response_id(value: str) -> bool:
    response_id = str(value or "").strip()
    if not response_id:
        return False
    return re.match(
        r"(?i)^(?:offline(?:[_-]|$)|mock(?:[_-]|$)|fake(?:[_-]|$)|fixture(?:[_-]|$)|test(?:[_-]|$))",
        response_id,
    ) is None


def _relay_approval_problems(
    approval_row: sqlite3.Row | None,
    envelope: dict[str, Any],
    approval_event_types: list[str],
) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    evidence: dict[str, Any] = {
        "status": "missing",
        "decided_at": "",
        "intent_sha256": "",
        "requested_event_count": approval_event_types.count("approval.requested"),
        "approved_event_count": approval_event_types.count("approval.approved"),
    }
    if approval_row is None:
        return ["relay_durable_approval_missing"], evidence

    try:
        payload = json.loads(str(approval_row["payload_json"] or ""))
    except json.JSONDecodeError:
        payload = {}
        problems.append("relay_approval_payload_invalid")
    if not isinstance(payload, dict):
        payload = {}
        problems.append("relay_approval_payload_invalid")

    expected_intent = hashlib.sha256(
        canonical_json(envelope).encode("utf-8")
    ).hexdigest()
    expected_hashes = [
        str(item.get("sha256") or "")
        for item in envelope.get("inputs", [])
        if isinstance(item, dict) and item.get("sha256")
    ]
    envelope_payload = (
        envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    )
    observed_intent = str(payload.get("intent_sha256") or "").strip().lower()
    evidence = {
        "approval_id": str(approval_row["approval_id"] or ""),
        "status": str(approval_row["status"] or ""),
        "created_at": str(approval_row["created_at"] or ""),
        "decided_at": str(approval_row["decided_at"] or ""),
        "intent_sha256": observed_intent,
        "requested_event_count": approval_event_types.count("approval.requested"),
        "approved_event_count": approval_event_types.count("approval.approved"),
    }
    if envelope.get("approval_policy") != "required":
        problems.append("relay_approval_policy_not_required")
    if approval_row["status"] != "approved" or not approval_row["decided_at"]:
        problems.append("relay_durable_approval_not_approved")
    if not str(approval_row["approval_id"] or "").strip():
        problems.append("relay_approval_id_missing")
    if not str(approval_row["created_at"] or "").strip():
        problems.append("relay_approval_created_at_missing")
    if not str(approval_row["reason"] or "").strip():
        problems.append("relay_approval_reason_missing")
    if payload.get("approval_id") != approval_row["approval_id"]:
        problems.append("relay_approval_id_binding_invalid")
    if payload.get("task_id") != envelope.get("task_id"):
        problems.append("relay_approval_task_binding_invalid")
    if payload.get("status") != "pending":
        problems.append("relay_approval_request_state_invalid")
    if payload.get("reason") != approval_row["reason"]:
        problems.append("relay_approval_reason_binding_invalid")
    if payload.get("artifact_hashes") != expected_hashes:
        problems.append("relay_approval_artifact_binding_invalid")
    if observed_intent != expected_intent:
        problems.append("relay_approval_intent_sha256_invalid")
    if payload.get("route_revision") != envelope_payload.get("route_revision"):
        problems.append("relay_approval_route_revision_binding_invalid")
    if approval_event_types.count("approval.requested") != 1:
        problems.append("relay_approval_requested_event_invalid")
    if approval_event_types.count("approval.approved") != 1:
        problems.append("relay_approval_approved_event_invalid")
    return problems, evidence


def _prompt_checks() -> list[dict[str, Any]]:
    contract_path = ROOT / "tests" / "contracts" / "v1_internal_prompts.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    total = 0
    for stage in contract["stages"].values():
        prompt_root = Path(stage["root"])
        expected = stage["files"]
        observed_names = {path.name for path in prompt_root.glob("*.md")} if prompt_root.is_dir() else set()
        if observed_names != set(expected):
            mismatches.append(str(prompt_root))
        for name, digest in expected.items():
            path = prompt_root / name
            if not path.is_file() or _sha256(path).upper() != str(digest).upper():
                mismatches.append(str(path))
            total += 1
    return [_check("internal_prompts_20", total == 20 and not mismatches, count=total, mismatches=mismatches)]


def _exe_checks() -> list[dict[str, Any]]:
    exe = ROOT / "dist" / "UnifiedPaperOrchestrator.exe"
    dist_files = sorted(path.name for path in exe.parent.iterdir() if path.is_file()) if exe.parent.is_dir() else []
    if not exe.is_file():
        return [_check("packaged_exe", False, path=str(exe), dist_files=dist_files)]
    completed = subprocess.run(
        [str(exe), "--version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    version_output = (completed.stdout + completed.stderr).strip()
    help_completed = subprocess.run(
        [str(exe), "gateway-retry", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    help_output = help_completed.stdout + help_completed.stderr
    relay_help_completed = subprocess.run(
        [str(exe), "gateway-relay-review-canary", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    relay_help_output = relay_help_completed.stdout + relay_help_completed.stderr
    codex_relay_help_completed = subprocess.run(
        [str(exe), "gateway-codex-relay-review-canary", "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    codex_relay_help_output = (
        codex_relay_help_completed.stdout + codex_relay_help_completed.stderr
    )
    critical_inputs = [
        *sorted((ROOT / "unified_paper_orchestrator").rglob("*.py")),
        *sorted((ROOT / "unified_paper_orchestrator_v2").rglob("*.py")),
        *sorted((ROOT / "prompts").rglob("*")),
        ROOT / "pyproject.toml",
        ROOT / "tools" / "windows_version_info.txt",
    ]
    critical_inputs = [path for path in critical_inputs if path.is_file()]
    newest_input = max(critical_inputs, key=lambda path: path.stat().st_mtime_ns)
    exe_fresh = exe.stat().st_mtime_ns >= newest_input.stat().st_mtime_ns
    escaped_exe = str(exe).replace("'", "''")
    version_info_completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                f"$v=(Get-Item -LiteralPath '{escaped_exe}').VersionInfo;"
                "@{FileVersion=$v.FileVersion;ProductVersion=$v.ProductVersion}"
                "|ConvertTo-Json -Compress"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    try:
        version_info = json.loads(version_info_completed.stdout)
    except json.JSONDecodeError:
        version_info = {}
    windows_version_ok = (
        version_info_completed.returncode == 0
        and str(version_info.get("FileVersion") or "").startswith(EXPECTED_VERSION)
        and str(version_info.get("ProductVersion") or "").startswith(EXPECTED_VERSION)
    )
    gateway_status = subprocess.run(
        [str(exe), "gateway-status"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    try:
        gateway_payload = json.loads(gateway_status.stdout)
    except json.JSONDecodeError:
        gateway_payload = {}
    write_boundary_ok = (
        gateway_status.returncode == 0
        and gateway_payload.get("canonical_writes_enabled") is False
        and gateway_payload.get("canonical_write_owner") == "v1_existing_workflows"
        and gateway_payload.get("canonical_write_switch") == "backend_router_legacy_only"
        and gateway_payload.get("canonical_gate_installed") is True
        and gateway_payload.get("canonical_gate_authorized") is False
        and gateway_payload.get("canonical_route_enabled") is False
        and gateway_payload.get("canonical_production_writes_enabled") is False
    )
    transport_boundary_ok = (
        gateway_status.returncode == 0
        and gateway_payload.get("generic_proxy_environment_ignored") is True
        and gateway_payload.get("explicit_proxy_scope")
        == "credential_free_http_loopback_only"
        and gateway_payload.get("review_transport_policies")
        == ["direct_no_env_proxy", "explicit_loopback_proxy_no_env"]
        and gateway_payload.get("codex_cli_transport_policies")
        == [
            "pinned_native_codex_cli_direct_no_env_proxy",
            "pinned_native_codex_cli_explicit_loopback_proxy_no_env",
        ]
        and gateway_payload.get("automatic_executor_fallback_enabled") is False
    )
    return [
        _check(
            "packaged_exe",
            completed.returncode == 0
            and EXPECTED_VERSION in version_output
            and dist_files == ["UnifiedPaperOrchestrator.exe"],
            path=str(exe),
            sha256=_sha256(exe),
            version_output=version_output,
            dist_files=dist_files,
        ),
        _check(
            "packaged_exe_freshness",
            exe_fresh,
            exe_mtime_ns=exe.stat().st_mtime_ns,
            newest_packaged_input=str(newest_input),
            newest_input_mtime_ns=newest_input.stat().st_mtime_ns,
        ),
        _check(
            "packaged_windows_version",
            windows_version_ok,
            file_version=version_info.get("FileVersion"),
            product_version=version_info.get("ProductVersion"),
        ),
        _check(
            "packaged_gateway_retry_contract",
            help_completed.returncode == 0
            and "--acknowledge-possible-duplicate-charge" in help_output,
        ),
        _check(
            "packaged_relay_review_contract",
            relay_help_completed.returncode == 0
            and "Relay model claim" in relay_help_output
            and "--max-output-tokens" in relay_help_output,
        ),
        _check(
            "packaged_codex_relay_review_contract",
            codex_relay_help_completed.returncode == 0
            and "standalone native codex executable"
            in codex_relay_help_output.lower()
            and "post-hoc" in codex_relay_help_output.lower(),
        ),
        _check(
            "single_canonical_writer_boundary",
            write_boundary_ok,
            gateway_canonical_writes=gateway_payload.get("canonical_writes_enabled"),
            canonical_write_owner=gateway_payload.get("canonical_write_owner"),
            canonical_write_switch=gateway_payload.get("canonical_write_switch"),
            canonical_gate_installed=gateway_payload.get("canonical_gate_installed"),
            canonical_gate_authorized=gateway_payload.get("canonical_gate_authorized"),
            canonical_route_enabled=gateway_payload.get("canonical_route_enabled"),
            canonical_production_writes_enabled=gateway_payload.get(
                "canonical_production_writes_enabled"
            ),
        ),
        _check(
            "bounded_openai_transport_boundary",
            transport_boundary_ok,
            generic_proxy_environment_ignored=gateway_payload.get(
                "generic_proxy_environment_ignored"
            ),
            explicit_proxy_scope=gateway_payload.get("explicit_proxy_scope"),
            review_transport_policies=gateway_payload.get(
                "review_transport_policies"
            ),
        ),
    ]


def _live_canary_check(
    *,
    expected_task_id: str = "",
    expected_paper_id: str = PAPER_ID,
) -> dict[str, Any]:
    database = ROOT / "state" / "v2" / "orchestrator.sqlite3"
    if not database.is_file():
        return _check("live_api_canary", False, status="incomplete", reason="gateway_database_missing")
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    approval_row: sqlite3.Row | None = None
    approval_event_types: list[str] = []
    try:
        columns = {
            str(item["name"])
            for item in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if not {"dispatch_owner", "execution_attempt_token_sha256"}.issubset(columns):
            return _check(
                "live_api_canary",
                False,
                status="incomplete",
                reason="gateway_database_requires_044_migration",
            )
        rows = connection.execute(
            """
            SELECT task_id, run_id, paper_id, action, executor, status,
                   dispatch_owner, execution_attempt_token_sha256,
                   envelope_json, result_json, created_at, updated_at
            FROM tasks
            WHERE paper_id=? AND (
                (action=? AND executor=?) OR
                (action=? AND executor=?) OR
                (action=? AND executor=?)
            )
            ORDER BY created_at DESC, task_id DESC
            """,
            (
                expected_paper_id,
                OFFICIAL_ACTION,
                OFFICIAL_EXECUTOR,
                RELAY_ACTION,
                RELAY_EXECUTOR,
                CODEX_CLI_RELAY_ACTION,
                CODEX_CLI_RELAY_EXECUTOR,
            ),
        ).fetchall()
        if expected_task_id:
            rows = [item for item in rows if item["task_id"] == expected_task_id]
        if rows:
            selected = next(
                (item for item in rows if item["status"] == "completed"),
                rows[0],
            )
            if _route_kind(selected["action"], selected["executor"]) in {
                RELAY_ROUTE_KIND,
                CODEX_CLI_RELAY_ROUTE_KIND,
            }:
                approval_row = connection.execute(
                    """
                    SELECT approval_id, task_id, status, reason, payload_json,
                           created_at, decided_at, decision_note
                    FROM approvals
                    WHERE task_id=?
                    """,
                    (selected["task_id"],),
                ).fetchone()
                approval_event_types = [
                    str(item["event_type"])
                    for item in connection.execute(
                        "SELECT event_type FROM events WHERE task_id=? ORDER BY event_id",
                        (selected["task_id"],),
                    ).fetchall()
                ]
    finally:
        connection.close()
    if not rows:
        return _check(
            "live_api_canary",
            False,
            status="incomplete",
            reason=(
                "expected_canary_task_not_found"
                if expected_task_id
                else "no_real_canary_task"
            ),
            expected_task_id=expected_task_id,
            expected_paper_id=expected_paper_id,
        )

    row = next((item for item in rows if item["status"] == "completed"), rows[0])
    problems: list[str] = []
    try:
        envelope = json.loads(row["envelope_json"])
        result = json.loads(row["result_json"]) if row["result_json"] else {}
    except json.JSONDecodeError:
        envelope = {}
        result = {}
        problems.append("task_record_json_invalid")
    if not isinstance(envelope, dict) or not isinstance(result, dict):
        envelope = envelope if isinstance(envelope, dict) else {}
        result = result if isinstance(result, dict) else {}
        problems.append("task_record_json_invalid")

    route_kind = _route_kind(row["action"], row["executor"])
    if route_kind == OFFICIAL_ROUTE_KIND:
        expected_action = OFFICIAL_ACTION
        expected_executor = OFFICIAL_EXECUTOR
        expected_provider_kind = OFFICIAL_PROVIDER_KIND
        expected_provider_profile = OFFICIAL_PROVIDER_PROFILE
        expected_provider_assurance = OFFICIAL_PROVIDER_ASSURANCE
    elif route_kind == RELAY_ROUTE_KIND:
        expected_action = RELAY_ACTION
        expected_executor = RELAY_EXECUTOR
        expected_provider_kind = RELAY_PROVIDER_KIND
        expected_provider_profile = RELAY_PROVIDER_PROFILE
        expected_provider_assurance = RELAY_PROVIDER_ASSURANCE
    elif route_kind == CODEX_CLI_RELAY_ROUTE_KIND:
        expected_action = CODEX_CLI_RELAY_ACTION
        expected_executor = CODEX_CLI_RELAY_EXECUTOR
        expected_provider_kind = CODEX_CLI_PROVIDER_KIND
        expected_provider_profile = CODEX_CLI_PROVIDER_PROFILE
        expected_provider_assurance = CODEX_CLI_PROVIDER_ASSURANCE
    else:
        expected_action = ""
        expected_executor = ""
        expected_provider_kind = ""
        expected_provider_profile = ""
        expected_provider_assurance = ""
        problems.append("canary_route_invalid")

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    envelope_payload = (
        envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    )
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    external_ref = str(result.get("external_ref") or "")
    model = str(data.get("model") or "")
    validation = data.get("validation") if isinstance(data.get("validation"), dict) else {}
    outputs = result.get("outputs") if isinstance(result.get("outputs"), list) else []
    execution_attempt_token_sha256 = str(
        row["execution_attempt_token_sha256"] or ""
    ).strip().lower()

    if row["status"] != "completed" or result.get("status") != "completed":
        problems.append(f"task_status={row['status']}")
    if row["dispatch_owner"] != "gateway" or envelope.get("dispatch_owner") != "gateway":
        problems.append("gateway_dispatch_owner_missing")
    if (
        envelope.get("task_id") != row["task_id"]
        or envelope.get("run_id") != row["run_id"]
        or envelope.get("paper_id") != expected_paper_id
        or envelope.get("stage") != "writing"
        or envelope.get("action") != expected_action
        or envelope.get("executor") != expected_executor
    ):
        problems.append("task_envelope_route_binding_invalid")
    if route_kind == OFFICIAL_ROUTE_KIND:
        if not external_ref.startswith("resp_") or external_ref.startswith(
            "offline_replay_"
        ):
            problems.append("real_response_id_missing")
    elif route_kind == RELAY_ROUTE_KIND and not _is_non_offline_response_id(
        external_ref
    ):
        problems.append("relay_response_id_missing_or_offline")
    elif (
        route_kind == CODEX_CLI_RELAY_ROUTE_KIND
        and CODEX_THREAD_ID.fullmatch(external_ref) is None
    ):
        problems.append("codex_cli_thread_id_invalid")
    if not model or model.startswith("offline-"):
        problems.append("real_model_missing")
    if (
        data.get("provider_kind") != expected_provider_kind
        or data.get("provider_profile") != expected_provider_profile
        or data.get("provider_assurance") != expected_provider_assurance
    ):
        problems.append("provider_provenance_binding_invalid")
    if route_kind == OFFICIAL_ROUTE_KIND and data.get("endpoint_origin") != OFFICIAL_ENDPOINT:
        problems.append("official_endpoint_not_proven")
    if route_kind == RELAY_ROUTE_KIND:
        payload = envelope_payload
        if set(payload) != RELAY_REVIEW_PAYLOAD_KEYS:
            problems.append("relay_payload_contract_invalid")
        expected_relay_payload = {
            "provider_kind": RELAY_PROVIDER_KIND,
            "provider_profile": RELAY_PROVIDER_PROFILE,
            "provider_assurance": RELAY_PROVIDER_ASSURANCE,
            "wire_api": RELAY_WIRE_API,
            "endpoint_base_url": RELAY_ENDPOINT,
            "endpoint_config_sha256": RELAY_ENDPOINT_CONFIG_SHA256,
            "third_party_data_disclosure_ack": True,
        }
        if any(
            payload.get(key) != value
            for key, value in expected_relay_payload.items()
        ):
            problems.append("relay_endpoint_or_disclosure_binding_invalid")
        if (
            data.get("endpoint_origin") != RELAY_ENDPOINT
            or data.get("endpoint_base_url") != RELAY_ENDPOINT
            or data.get("endpoint_config_sha256")
            != RELAY_ENDPOINT_CONFIG_SHA256
        ):
            problems.append("relay_result_endpoint_binding_invalid")
        approval_problems, approval_evidence = _relay_approval_problems(
            approval_row,
            envelope,
            approval_event_types,
        )
        problems.extend(approval_problems)
    elif route_kind == CODEX_CLI_RELAY_ROUTE_KIND:
        payload = envelope_payload
        if set(payload) != CODEX_CLI_RELAY_REVIEW_PAYLOAD_KEYS:
            problems.append("codex_cli_payload_contract_invalid")
        if any(
            payload.get(key) != value
            for key, value in CODEX_CLI_PROVIDER_PAYLOAD.items()
        ):
            problems.append("codex_cli_provider_binding_invalid")
        if (
            re.fullmatch(r"[0-9a-f]{64}", str(payload.get("codex_cli_sha256") or ""))
            is None
            or re.fullmatch(
                r"[0-9a-f]{64}", str(payload.get("relay_key_fingerprint") or "")
            )
            is None
            or not Path(str(payload.get("codex_cli_path") or "")).is_absolute()
        ):
            problems.append("codex_cli_binary_or_credential_binding_invalid")
        if (
            data.get("endpoint_origin") != RELAY_ENDPOINT
            or data.get("endpoint_base_url") != RELAY_ENDPOINT
            or data.get("endpoint_config_sha256")
            != payload.get("endpoint_config_sha256")
            or any(
                data.get(key) != payload.get(key)
                for key in CODEX_CLI_ENVELOPE_RECEIPT_KEYS
            )
            or data.get("billing_credential_fingerprint")
            != payload.get("relay_key_fingerprint")
            or data.get("provider_external_ref_kind") != "codex_thread_id"
            or data.get("requested_model") != payload.get("model")
            or data.get("reported_model") != ""
            or data.get("provider_response_id") != ""
            or not str(data.get("codex_cli_version") or "")
            or metrics.get("usage_total_source")
            not in {"input_plus_output_v1", "reported_total_tokens"}
        ):
            problems.append("codex_cli_result_identity_binding_invalid")
        approval_problems, approval_evidence = _relay_approval_problems(
            approval_row,
            envelope,
            approval_event_types,
        )
        problems.extend(approval_problems)
    else:
        approval_evidence = {
            "status": "not_required",
            "decided_at": "",
            "intent_sha256": "",
            "requested_event_count": 0,
            "approved_event_count": 0,
        }
    allowed_transport_policies = (
        {
            "pinned_native_codex_cli_direct_no_env_proxy",
            "pinned_native_codex_cli_explicit_loopback_proxy_no_env",
        }
        if route_kind == CODEX_CLI_RELAY_ROUTE_KIND
        else {"direct_no_env_proxy", "explicit_loopback_proxy_no_env"}
    )
    if data.get("transport_policy") not in allowed_transport_policies:
        problems.append("bounded_transport_not_proven")
    if data.get("canonical_write") is not False:
        problems.append("non_destructive_flag_missing")
    if data.get("source_unchanged") is not True:
        problems.append("source_integrity_not_proven")
    if validation.get("ok") is not True:
        problems.append("output_validation_failed")
    total_tokens = metrics.get("total_tokens")
    if not isinstance(total_tokens, int) or total_tokens <= 0:
        problems.append("real_usage_missing")
    if len(outputs) != 2:
        problems.append("staging_pair_missing")
    if re.fullmatch(r"[0-9a-f]{64}", execution_attempt_token_sha256) is None:
        problems.append("execution_attempt_token_missing")

    staging_root = (ROOT / "state" / "v2" / "staging").resolve()
    expected_staging_dir = (
        staging_root / str(row["run_id"]) / str(row["task_id"])
    ).resolve()
    try:
        expected_staging_dir.relative_to(staging_root)
        staging_binding_safe = expected_staging_dir != staging_root
    except ValueError:
        staging_binding_safe = False
    if not staging_binding_safe:
        problems.append("staging_task_path_unsafe")
    if Path(str(data.get("staging_dir") or "")).resolve() != expected_staging_dir:
        problems.append("staging_directory_binding_invalid")
    expected_is_junction = getattr(expected_staging_dir, "is_junction", None)
    if (
        not staging_binding_safe
        or not expected_staging_dir.is_dir()
        or expected_staging_dir.is_symlink()
        or (callable(expected_is_junction) and expected_is_junction())
    ):
        problems.append("staging_directory_missing_or_unsafe")
    else:
        actual_names = {path.name for path in expected_staging_dir.iterdir()}
        if actual_names != {"review_round_1.md", "result.json"}:
            problems.append("staging_directory_file_set_invalid")
    output_evidence: list[dict[str, Any]] = []
    output_by_name: dict[str, tuple[Path, str]] = {}
    for artifact in outputs:
        if not isinstance(artifact, dict):
            problems.append("output_artifact_invalid")
            continue
        raw_path = Path(str(artifact.get("uri") or ""))
        is_junction = getattr(raw_path, "is_junction", None)
        if raw_path.is_symlink() or (callable(is_junction) and is_junction()):
            problems.append(f"output_reparse_point:{raw_path.name}")
            continue
        path = raw_path.resolve()
        try:
            path.relative_to(staging_root)
        except ValueError:
            problems.append("output_outside_staging")
            continue
        if path.parent != expected_staging_dir:
            problems.append(f"output_task_directory_mismatch:{path.name}")
            continue
        if not path.is_file():
            problems.append(f"output_missing:{path.name}")
            continue
        observed = _sha256(path)
        expected = str(artifact.get("sha256") or "")
        if observed != expected:
            problems.append(f"output_hash_mismatch:{path.name}")
        output_by_name[path.name] = (path, observed)
        output_evidence.append({"name": path.name, "path": str(path), "sha256": observed})

    if set(output_by_name) != {"review_round_1.md", "result.json"}:
        problems.append("staging_pair_names_invalid")
    else:
        manifest_path = output_by_name["result.json"][0]
        review_path = output_by_name["review_round_1.md"][0]
        try:
            staged_review = review_path.read_text(encoding="utf-8")
            observed_validation = validate_review_markdown(
                staged_review,
                expected_figure_count=int(
                    envelope_payload.get("expected_figure_count") or 0
                ),
                expected_table_count=int(
                    envelope_payload.get("expected_table_count") or 0
                ),
            )
        except (OSError, UnicodeError, TypeError, ValueError):
            observed_validation = {}
            problems.append("staged_review_validator_unavailable")
        if observed_validation != validation or observed_validation.get("ok") is not True:
            problems.append("staged_review_validator_binding_invalid")
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest = {}
            problems.append("staging_manifest_unreadable")
        if not isinstance(manifest, dict):
            manifest = {}
            problems.append("staging_manifest_invalid")
        if manifest and canonical_json(manifest).encode("utf-8") + b"\n" != manifest_bytes:
            problems.append("staging_manifest_not_canonical")
        expected_input_hashes = {
            role: value.get("sha256", "")
            for role, value in envelope_payload.get("input_roles", {}).items()
            if isinstance(value, dict)
        } if isinstance(envelope_payload.get("input_roles"), dict) else {}
        expected_manifest_values = {
            "schema_version": (
                "review_canary_result_v3"
                if route_kind == CODEX_CLI_RELAY_ROUTE_KIND
                else "review_canary_result_v2"
            ),
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "paper_id": expected_paper_id,
            "action": expected_action,
            "executor": expected_executor,
            "model": model,
            "endpoint_origin": data.get("endpoint_origin"),
            "transport_policy": data.get("transport_policy"),
            "provider_kind": expected_provider_kind,
            "provider_profile": expected_provider_profile,
            "provider_assurance": expected_provider_assurance,
            "source_prompt_sha256": envelope_payload.get(
                "source_prompt_sha256", ""
            ),
            "rendered_prompt_sha256": envelope_payload.get(
                "rendered_prompt_sha256", ""
            ),
            "prompt_transform_id": envelope_payload.get(
                "prompt_transform_id", ""
            ),
            "outer_adapter_version": envelope_payload.get(
                "outer_adapter_version", ""
            ),
            "token_estimator_id": envelope_payload.get(
                "token_estimator_id", ""
            ),
            "evidence_scope": envelope_payload.get("evidence_scope", ""),
            "input_manifest_sha256": envelope_payload.get(
                "input_manifest_sha256", ""
            ),
            "execution_attempt_token_sha256": execution_attempt_token_sha256,
            "input_hashes": expected_input_hashes,
            "usage": metrics,
            "validation": validation,
            "source_unchanged": True,
            "canonical_write": False,
            **(
                {
                    "provider_external_ref_kind": "codex_thread_id",
                    "provider_external_ref": external_ref,
                    "approved_codex_cli_path": envelope_payload.get(
                        "codex_cli_path", ""
                    ),
                    "minimum_codex_cli_version": envelope_payload.get(
                        "minimum_codex_cli_version", ""
                    ),
                }
                if route_kind == CODEX_CLI_RELAY_ROUTE_KIND
                else {"response_id": external_ref}
            ),
        }
        if route_kind == RELAY_ROUTE_KIND:
            expected_manifest_values.update(
                {
                    "endpoint_base_url": RELAY_ENDPOINT,
                    "endpoint_config_sha256": RELAY_ENDPOINT_CONFIG_SHA256,
                }
            )
        if route_kind == CODEX_CLI_RELAY_ROUTE_KIND:
            expected_manifest_values.update(
                {
                    "endpoint_base_url": RELAY_ENDPOINT,
                    "endpoint_config_sha256": envelope_payload.get(
                        "endpoint_config_sha256", ""
                    ),
                    **{
                        key: envelope_payload.get(key)
                        for key in CODEX_CLI_ENVELOPE_RECEIPT_KEYS
                    },
                    **{
                        key: data.get(key)
                        for key in CODEX_CLI_RUNTIME_RECEIPT_KEYS
                    },
                }
            )
        required_manifest_keys = {
            *expected_manifest_values,
            "output",
        }
        if set(manifest) != required_manifest_keys or any(
            manifest.get(key) != value for key, value in expected_manifest_values.items()
        ):
            problems.append("staging_manifest_task_binding_invalid")
        manifest_output = manifest.get("output") if isinstance(manifest.get("output"), dict) else {}
        review_sha = output_by_name["review_round_1.md"][1]
        if (
            manifest_output.get("path") != str(review_path)
            or manifest_output.get("sha256") != review_sha
            or manifest_output.get("bytes") != review_path.stat().st_size
        ):
            problems.append("staging_manifest_output_binding_invalid")

    for artifact in envelope.get("inputs", []):
        if not isinstance(artifact, dict):
            problems.append("input_artifact_invalid")
            continue
        path = Path(str(artifact.get("uri") or ""))
        expected = str(artifact.get("sha256") or "")
        if not path.is_file() or _sha256(path) != expected:
            problems.append(f"input_hash_mismatch:{path.name}")

    historical_review = Path(WRITING_ROOT) / "papers" / expected_paper_id / "review_round_1.md"
    historical_sha = _sha256(historical_review) if historical_review.is_file() else ""
    if historical_sha != KNOWN_HISTORICAL_REVIEW_SHA256:
        problems.append("historical_review_changed")
    exe = ROOT / "dist" / "UnifiedPaperOrchestrator.exe"
    try:
        completed_timestamp = datetime.fromisoformat(str(row["updated_at"])).timestamp()
    except (TypeError, ValueError):
        completed_timestamp = 0.0
        problems.append("canary_completed_at_invalid")
    exe_mtime = exe.stat().st_mtime if exe.is_file() else 0.0
    if completed_timestamp < exe_mtime:
        problems.append("canary_completion_predates_current_executable")

    official_endpoint_proven = (
        route_kind == OFFICIAL_ROUTE_KIND
        and data.get("endpoint_origin") == OFFICIAL_ENDPOINT
        and data.get("provider_kind") == OFFICIAL_PROVIDER_KIND
        and data.get("provider_profile") == OFFICIAL_PROVIDER_PROFILE
        and data.get("provider_assurance") == OFFICIAL_PROVIDER_ASSURANCE
    )
    candidate_evidence = [
        {
            "task_id": item["task_id"],
            "status": item["status"],
            "route_kind": _route_kind(item["action"], item["executor"]),
            "created_at": item["created_at"],
        }
        for item in rows
    ]

    return _check(
        "live_api_canary",
        not problems,
        status="failed" if rows else "incomplete",
        task_id=row["task_id"],
        run_id=row["run_id"],
        task_status=row["status"],
        model=model,
        response_id=(
            "" if route_kind == CODEX_CLI_RELAY_ROUTE_KIND else external_ref
        ),
        provider_external_ref_kind=(
            "codex_thread_id"
            if route_kind == CODEX_CLI_RELAY_ROUTE_KIND
            else "responses_api_response_id"
        ),
        provider_external_ref=external_ref,
        gateway_request_id=str(data.get("gateway_request_id") or ""),
        route_kind=route_kind,
        provider_kind=str(data.get("provider_kind") or ""),
        provider_profile=str(data.get("provider_profile") or ""),
        provider_assurance=str(data.get("provider_assurance") or ""),
        endpoint_origin=str(data.get("endpoint_origin") or ""),
        endpoint_config_sha256=str(data.get("endpoint_config_sha256") or ""),
        official_openai_endpoint_proven=official_endpoint_proven,
        upstream_model_identity_verified=official_endpoint_proven,
        chatgpt_subscription_used=bool(
            data.get("chatgpt_subscription_used", False)
        ),
        openai_api_account_used=bool(data.get("openai_api_account_used", False)),
        approval=approval_evidence,
        total_tokens=total_tokens,
        historical_review_sha256=historical_sha,
        candidates=candidate_evidence,
        outputs=output_evidence,
        problems=problems,
        expected_task_id=expected_task_id,
        canary_completed_timestamp=completed_timestamp,
        executable_mtime=exe_mtime,
    )


def verify(
    *,
    expected_canary_task_id: str = "",
    expected_paper_id: str = PAPER_ID,
) -> dict[str, Any]:
    checks = [
        _check("source_version", __version__ == EXPECTED_VERSION, observed=__version__),
        *_prompt_checks(),
        *_exe_checks(),
        _live_canary_check(
            expected_task_id=expected_canary_task_id,
            expected_paper_id=expected_paper_id,
        ),
    ]
    failed = [item for item in checks if not item["ok"] and item["status"] == "failed"]
    incomplete = [item for item in checks if not item["ok"] and item["status"] == "incomplete"]
    status = "FAIL" if failed else "INCOMPLETE" if incomplete else "PASS"
    return {
        "schema_version": "release_044_completion_v1",
        "status": status,
        "checks": checks,
        "failed_count": len(failed),
        "incomplete_count": len(incomplete),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the exact 0.4.4 release evidence set.")
    parser.add_argument("--expected-canary-task-id", default="")
    parser.add_argument("--expected-paper-id", default=PAPER_ID)
    args = parser.parse_args()
    result = verify(
        expected_canary_task_id=args.expected_canary_task_id,
        expected_paper_id=args.expected_paper_id,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "PASS" else 2 if result["status"] == "INCOMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
