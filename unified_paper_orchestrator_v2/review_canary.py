from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from .config import STATE_DIR
from .contracts import ApprovalPolicy, ArtifactRef, TaskEnvelope, canonical_json
from .executors.codex_cli_relay import (
    CODEX_CLI_ARGV_POLICY,
    CODEX_CLI_BILLING_POLICY,
    CODEX_CLI_CLIENT_KIND,
    CODEX_CLI_CLIENT_PROVENANCE_ASSURANCE,
    CODEX_CLI_CONFIG_PROFILE,
    CODEX_CLI_CREDENTIAL_POLICY,
    CODEX_CLI_ENV_POLICY,
    CODEX_CLI_EXECUTABLE_ENV,
    CODEX_CLI_FALLBACK_POLICY,
    CODEX_CLI_IDENTITY_POLICY,
    CODEX_CLI_JSONL_POLICY,
    CODEX_CLI_MIN_VERSION,
    CODEX_CLI_OUTPUT_LIMIT_POLICY,
    CODEX_CLI_PROVIDER_KIND,
    CODEX_CLI_PROVIDER_PROFILE,
    CODEX_CLI_REASONING_EFFORT,
    CODEX_CLI_REASONING_NAMESPACE,
    codex_cli_argv_policy_sha256,
    codex_cli_static_config_sha256,
    relay_key_fingerprint,
    resolve_native_codex_cli,
    sha256_file,
)
from .executors.openai_relay import (
    QILIN_RELAY_BASE_URL,
    QILIN_RELAY_PROVIDER_ASSURANCE,
    QILIN_RELAY_PROVIDER_KIND,
    QILIN_RELAY_PROVIDER_PROFILE,
    QILIN_RELAY_WIRE_API,
    RELAY_MODEL_ENV,
    RELAY_API_KEY_ENV,
    relay_endpoint_config_sha256,
)
from .provider_runtime import RuntimeProviderConfig, provider_config_digest


REVIEW_ACTION = "writing.review_round_1.canary"
RELAY_REVIEW_ACTION = "writing.review_round_1.compatible_relay_canary"
CODEX_CLI_RELAY_REVIEW_ACTION = "writing.review_round_1.qilin_codex_cli_canary"
CODEX_SDK_PROFILE_REVIEW_ACTION = "writing.review_round_1.codex_sdk_profile_canary"
REVIEW_ROUTE_EXECUTORS = {
    REVIEW_ACTION: "openai_review",
    RELAY_REVIEW_ACTION: "openai_compatible_relay_review",
    CODEX_CLI_RELAY_REVIEW_ACTION: "qilin_codex_cli_review",
    CODEX_SDK_PROFILE_REVIEW_ACTION: "codex_sdk",
}
REVIEW_ACTIONS = frozenset(REVIEW_ROUTE_EXECUTORS)
RELAY_REVIEW_ACTIONS = frozenset(
    {
        RELAY_REVIEW_ACTION,
        CODEX_CLI_RELAY_REVIEW_ACTION,
        CODEX_SDK_PROFILE_REVIEW_ACTION,
    }
)
REVIEW_PROMPT_FILENAME = "07_review_round_1.md"
PROMPT_SKILL_DIRECTIVE = "$academic-paper-reviewer"
PROMPT_TRANSFORM_ID = "prompt_declared_skill_fallback_v1"
OUTER_ADAPTER_VERSION = "review_canary_adapter_v2"
TOKEN_ESTIMATOR_ID = "utf8_bytes_div_3_ceiling_v1"
EVIDENCE_SCOPE = "tex_source_only_no_rendered_figures"
DEFAULT_STAGING_ROOT = STATE_DIR / "staging"
_PAPER_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{3}_[A-Za-z0-9][A-Za-z0-9_-]*$")

REQUIRED_HEADINGS = (
    "# Review Round 1",
    "## Overall Verdict",
    "## One-sentence Reason",
    "## Summary",
    "## Strengths",
    "## Major Weaknesses",
    "## Rejection-level Issues",
    "## Reviewer A: Systems Review",
    "## Reviewer B: ML Efficiency Review",
    "## Reviewer C: Novelty Review",
    "## Reviewer D: Writing and Presentation Review",
    "## Reviewer E: Figure and Evidence Review",
    "## Novelty Concerns",
    "## Method Concerns",
    "## Experiment Concerns",
    "## Baseline Concerns",
    "## Figure and Quantitative Evidence Review",
    "## Figure and Table Concerns",
    "## Citation and Related Work Concerns",
    "## Claim-Evidence Problems",
    "## Overclaiming Problems",
    "## Section-by-section Comments",
    "## Required Revisions",
    "## Optional Revisions",
    "## Final Action Plan",
)
ALLOWED_VERDICTS = {
    "Strong Accept",
    "Accept",
    "Weak Accept",
    "Borderline",
    "Weak Reject",
    "Reject",
}
FIGURE_TABLE_HEADER = (
    "| Fig. | Caption Summary | Current Type | Conceptual? | Quantitative? | Table-like? | "
    "Keep / Convert / Replace | Recommended New Type | Required Data / CSV Schema |"
)
INPUT_ORDER = (
    "idea",
    "references",
    "literature_map",
    "novelty_check",
    "draft_v3",
    "source_prompt",
)
REVIEW_PAYLOAD_KEYS = frozenset(
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
RELAY_PROVIDER_PAYLOAD_KEYS = frozenset(
    {
        "provider_kind",
        "provider_profile",
        "provider_assurance",
        "wire_api",
        "endpoint_base_url",
        "endpoint_config_sha256",
        "third_party_data_disclosure_ack",
    }
)
RELAY_REVIEW_PAYLOAD_KEYS = REVIEW_PAYLOAD_KEYS | RELAY_PROVIDER_PAYLOAD_KEYS
CODEX_CLI_RELAY_PROVIDER_PAYLOAD_KEYS = frozenset(
    {
        "provider_kind",
        "provider_profile",
        "provider_assurance",
        "wire_api",
        "endpoint_base_url",
        "endpoint_config_sha256",
        "third_party_data_disclosure_ack",
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
        "fallback_policy",
        "output_token_limit_policy",
        "minimum_codex_cli_version",
        "argv_policy",
        "argv_policy_sha256",
        "environment_policy",
        "jsonl_policy",
        "codex_cli_path",
        "codex_cli_sha256",
        "relay_key_fingerprint",
    }
)
CODEX_CLI_RELAY_REVIEW_PAYLOAD_KEYS = (
    REVIEW_PAYLOAD_KEYS | CODEX_CLI_RELAY_PROVIDER_PAYLOAD_KEYS
)


def relay_review_provider_payload() -> dict[str, Any]:
    return {
        "provider_kind": QILIN_RELAY_PROVIDER_KIND,
        "provider_profile": QILIN_RELAY_PROVIDER_PROFILE,
        "provider_assurance": QILIN_RELAY_PROVIDER_ASSURANCE,
        "wire_api": QILIN_RELAY_WIRE_API,
        "endpoint_base_url": QILIN_RELAY_BASE_URL,
        "endpoint_config_sha256": relay_endpoint_config_sha256(),
        "third_party_data_disclosure_ack": True,
    }


def codex_cli_relay_static_provider_payload() -> dict[str, Any]:
    return {
        "provider_kind": CODEX_CLI_PROVIDER_KIND,
        "provider_profile": CODEX_CLI_PROVIDER_PROFILE,
        "provider_assurance": QILIN_RELAY_PROVIDER_ASSURANCE,
        "wire_api": QILIN_RELAY_WIRE_API,
        "endpoint_base_url": QILIN_RELAY_BASE_URL,
        "endpoint_config_sha256": codex_cli_static_config_sha256(),
        "third_party_data_disclosure_ack": True,
        "client_kind": CODEX_CLI_CLIENT_KIND,
        "client_provenance_assurance": CODEX_CLI_CLIENT_PROVENANCE_ASSURANCE,
        "client_config_profile": CODEX_CLI_CONFIG_PROFILE,
        "reasoning_effort": CODEX_CLI_REASONING_EFFORT,
        "reasoning_effort_namespace": CODEX_CLI_REASONING_NAMESPACE,
        "identity_policy": CODEX_CLI_IDENTITY_POLICY,
        "credential_policy": CODEX_CLI_CREDENTIAL_POLICY,
        "billing_policy": CODEX_CLI_BILLING_POLICY,
        "billing_account_verified": False,
        "chatgpt_subscription_used": False,
        "openai_api_account_used": False,
        "official_openai_endpoint_proven": False,
        "upstream_model_identity_verified": False,
        "model_identity_assurance": "relay_claim_unverified",
        "fallback_policy": CODEX_CLI_FALLBACK_POLICY,
        "output_token_limit_policy": CODEX_CLI_OUTPUT_LIMIT_POLICY,
        "minimum_codex_cli_version": CODEX_CLI_MIN_VERSION,
        "argv_policy": CODEX_CLI_ARGV_POLICY,
        "argv_policy_sha256": codex_cli_argv_policy_sha256(),
        "environment_policy": CODEX_CLI_ENV_POLICY,
        "jsonl_policy": CODEX_CLI_JSONL_POLICY,
    }


def codex_sdk_profile_provider_payload(
    config: RuntimeProviderConfig,
) -> dict[str, Any]:
    config.validate()
    return {
        "provider_kind": "custom_codex_responses",
        "provider_profile": config.profile_id,
        "provider_assurance": "user_configured_endpoint_unverified",
        "wire_api": config.wire_api,
        "endpoint_base_url": config.base_url,
        "endpoint_config_sha256": provider_config_digest(config),
        "third_party_data_disclosure_ack": True,
    }


def codex_cli_relay_task_provider_payload(
    *,
    codex_cli_path: Path | str | None = None,
) -> dict[str, Any]:
    executable_value = str(
        codex_cli_path
        if codex_cli_path is not None
        else os.environ.get(CODEX_CLI_EXECUTABLE_ENV, "")
    ).strip()
    try:
        executable = resolve_native_codex_cli(executable_value)
        executable_sha256 = sha256_file(executable)
    except (OSError, ValueError) as exc:
        raise ReviewCanaryError(
            "REVIEW_CANARY_CODEX_CLI_INVALID",
            "A pinned standalone native Codex CLI executable is required.",
        ) from exc
    credential_fingerprint = relay_key_fingerprint(
        os.environ.get(RELAY_API_KEY_ENV, "")
    )
    if not credential_fingerprint:
        raise ReviewCanaryError(
            "REVIEW_CANARY_RELAY_KEY_REQUIRED",
            "The relay credential must be present in memory before creating the approved CLI task.",
        )
    return {
        **codex_cli_relay_static_provider_payload(),
        "codex_cli_path": str(executable),
        "codex_cli_sha256": executable_sha256,
        "relay_key_fingerprint": credential_fingerprint,
    }


def review_payload_keys(action: str) -> frozenset[str]:
    if action in {RELAY_REVIEW_ACTION, CODEX_SDK_PROFILE_REVIEW_ACTION}:
        return RELAY_REVIEW_PAYLOAD_KEYS
    if action == CODEX_CLI_RELAY_REVIEW_ACTION:
        return CODEX_CLI_RELAY_REVIEW_PAYLOAD_KEYS
    return REVIEW_PAYLOAD_KEYS


def review_provider_payload_keys(action: str) -> frozenset[str]:
    if action in {RELAY_REVIEW_ACTION, CODEX_SDK_PROFILE_REVIEW_ACTION}:
        return RELAY_PROVIDER_PAYLOAD_KEYS
    if action == CODEX_CLI_RELAY_REVIEW_ACTION:
        return CODEX_CLI_RELAY_PROVIDER_PAYLOAD_KEYS
    return frozenset()


class ReviewCanaryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_link_like(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return True


def _path_has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    anchor = Path(absolute.anchor)
    try:
        relative = absolute.relative_to(anchor)
    except ValueError:
        return True
    current = anchor
    for part in relative.parts:
        current /= part
        if _is_link_like(current):
            return True
    return False


def _default_writing_root() -> Path:
    from unified_paper_orchestrator.config import WRITING_ROOT

    return Path(WRITING_ROOT)


def _input_paths(paper_id: str, writing_root: Path) -> dict[str, Path]:
    if not _PAPER_ID.fullmatch(str(paper_id or "")):
        raise ReviewCanaryError("REVIEW_CANARY_PAPER_ID_INVALID", "A complete existing writing paper ID is required.")
    raw_root = Path(os.path.abspath(os.fspath(writing_root)))
    if _path_has_link_component(raw_root):
        raise ReviewCanaryError(
            "REVIEW_CANARY_INPUT_REPARSE_REJECTED",
            "Writing root crosses a symbolic link or Windows reparse point.",
        )
    try:
        root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise ReviewCanaryError(
            "REVIEW_CANARY_INPUT_PATH_INVALID",
            "Writing root is unavailable.",
        ) from exc
    paths = {
        "idea": root / "ideas" / f"{paper_id}.md",
        "references": root / "refs" / paper_id / "references.md",
        "literature_map": root / "refs" / paper_id / "literature_map.md",
        "novelty_check": root / "refs" / paper_id / "novelty_check.md",
        "draft_v3": root / "papers" / paper_id / "draft_v3.tex",
        "source_prompt": root / "prompts" / REVIEW_PROMPT_FILENAME,
    }
    for role, path in paths.items():
        if _path_has_link_component(path):
            raise ReviewCanaryError(
                "REVIEW_CANARY_INPUT_REPARSE_REJECTED",
                f"Review input crosses a symbolic link or Windows reparse point: {role}",
            )
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_PATH_INVALID", f"Input path escapes writing root: {role}") from exc
        if not resolved.is_file():
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_MISSING", f"Required review input is missing: {role}")
        paths[role] = resolved
    return paths


def _render_prompt(source: str) -> str:
    lines = source.splitlines(keepends=True)
    if not lines or lines[0].strip() != PROMPT_SKILL_DIRECTIVE:
        raise ReviewCanaryError(
            "REVIEW_CANARY_PROMPT_FALLBACK_INVALID",
            "Prompt 07 no longer contains its declared non-skill fallback directive.",
        )
    return "".join(lines[1:])


def _draft_counts(text: str) -> tuple[int, int, int]:
    figures = len(re.findall(r"\\begin\{figure\*?\}", text))
    tables = len(re.findall(r"\\begin\{table\*?\}", text))
    bibitems = len(re.findall(r"\\bibitem(?:\[[^\]]*\])?\{", text))
    return figures, tables, bibitems


def build_review_canary_envelope(
    paper_id: str,
    *,
    executor: str = "openai_review",
    action: str = REVIEW_ACTION,
    model: str = "",
    writing_root: Path | None = None,
    staging_root: Path | None = None,
    token_budget: int = 50000,
    max_output_tokens: int = 8000,
    timeout_seconds: int = 600,
    codex_cli_path: Path | str | None = None,
    codex_sdk_provider_config: RuntimeProviderConfig | None = None,
) -> TaskEnvelope:
    if REVIEW_ROUTE_EXECUTORS.get(action) != executor:
        raise ReviewCanaryError(
            "REVIEW_CANARY_ROUTE_INVALID",
            "Review canary action and executor are not an allowed exact route pair.",
        )
    root = Path(writing_root) if writing_root is not None else _default_writing_root()
    staging = Path(staging_root) if staging_root is not None else DEFAULT_STAGING_ROOT
    paths = _input_paths(paper_id, root)
    raw_by_role = {role: paths[role].read_bytes() for role in INPUT_ORDER}
    source_prompt = raw_by_role["source_prompt"].decode("utf-8-sig")
    rendered_prompt = _render_prompt(source_prompt)
    draft_text = raw_by_role["draft_v3"].decode("utf-8-sig")
    figure_count, table_count, bibitem_count = _draft_counts(draft_text)

    artifacts: list[ArtifactRef] = []
    input_roles: dict[str, dict[str, str]] = {}
    for role in INPUT_ORDER:
        media_type = "text/x-tex" if role == "draft_v3" else "text/markdown"
        artifact = ArtifactRef.from_file(paths[role], media_type=media_type)
        artifacts.append(artifact)
        input_roles[role] = {"uri": artifact.uri, "sha256": artifact.sha256}

    manifest_payload = {role: input_roles[role]["sha256"] for role in INPUT_ORDER}
    input_manifest_sha256 = _sha256(canonical_json(manifest_payload).encode("utf-8"))
    model_env = RELAY_MODEL_ENV if action in RELAY_REVIEW_ACTIONS else "OPENAI_MODEL"
    resolved_model = str(model or os.environ.get(model_env) or "").strip()
    if action == CODEX_SDK_PROFILE_REVIEW_ACTION:
        if codex_sdk_provider_config is None:
            raise ReviewCanaryError(
                "REVIEW_CANARY_CODEX_SDK_PROFILE_REQUIRED",
                "A trusted Codex SDK provider profile is required.",
            )
        codex_sdk_provider_config.validate()
        if resolved_model and resolved_model != codex_sdk_provider_config.model:
            raise ReviewCanaryError(
                "REVIEW_CANARY_CODEX_SDK_MODEL_MISMATCH",
                "The review model must match the trusted provider profile.",
            )
        resolved_model = codex_sdk_provider_config.model
    if action == CODEX_CLI_RELAY_REVIEW_ACTION and not resolved_model:
        raise ReviewCanaryError(
            "REVIEW_CANARY_RELAY_MODEL_REQUIRED",
            "An explicit relay model must be bound before the Codex CLI task is created.",
        )
    provider_payload: dict[str, Any] = {}
    if action == RELAY_REVIEW_ACTION:
        provider_payload = relay_review_provider_payload()
    elif action == CODEX_CLI_RELAY_REVIEW_ACTION:
        provider_payload = codex_cli_relay_task_provider_payload(
            codex_cli_path=codex_cli_path,
        )
    elif action == CODEX_SDK_PROFILE_REVIEW_ACTION:
        assert codex_sdk_provider_config is not None
        provider_payload = codex_sdk_profile_provider_payload(
            codex_sdk_provider_config
        )
    schema = (
        "review_canary_codex_cli_relay_v1"
        if action == CODEX_CLI_RELAY_REVIEW_ACTION
        else "review_canary_codex_sdk_profile_v1"
        if action == CODEX_SDK_PROFILE_REVIEW_ACTION
        else "review_canary_relay_v1"
        if action == RELAY_REVIEW_ACTION
        else "review_canary_v1"
    )
    idempotency_payload = {
        "schema": schema,
        "action": action,
        "paper_id": paper_id,
        "executor": executor,
        "model": resolved_model or "unconfigured",
        "input_manifest_sha256": input_manifest_sha256,
        "input_locations_sha256": _sha256(
            canonical_json({role: input_roles[role]["uri"] for role in INPUT_ORDER}).encode("utf-8")
        ),
        "rendered_prompt_sha256": _sha256(rendered_prompt.encode("utf-8")),
        "outer_adapter_version": OUTER_ADAPTER_VERSION,
        "token_estimator_id": TOKEN_ESTIMATOR_ID,
        "token_budget": int(token_budget),
        "max_output_tokens": int(max_output_tokens),
        "timeout_seconds": int(timeout_seconds),
        "staging_root": str(staging.resolve()),
        "provider_binding": provider_payload,
    }
    idempotency_key = f"review-canary:{_sha256(canonical_json(idempotency_payload).encode('utf-8'))}"
    envelope = TaskEnvelope(
        paper_id=paper_id,
        stage="writing",
        action=action,
        executor=executor,
        idempotency_key=idempotency_key,
        prompt_version=f"sha256:{input_roles['source_prompt']['sha256']}",
        inputs=artifacts,
        payload={
            "model": resolved_model,
            "input_roles": input_roles,
            "source_prompt_sha256": input_roles["source_prompt"]["sha256"],
            "rendered_prompt_sha256": _sha256(rendered_prompt.encode("utf-8")),
            "input_manifest_sha256": input_manifest_sha256,
            "prompt_transform_id": PROMPT_TRANSFORM_ID,
            "outer_adapter_version": OUTER_ADAPTER_VERSION,
            "token_estimator_id": TOKEN_ESTIMATOR_ID,
            "evidence_scope": EVIDENCE_SCOPE,
            "staging_only": True,
            "max_output_tokens": int(max_output_tokens),
            "expected_figure_count": figure_count,
            "expected_table_count": table_count,
            "expected_bibitem_count": bibitem_count,
            **provider_payload,
        },
        allowed_tools=[],
        writable_roots=[str(staging.resolve())],
        network_access=True,
        token_budget=int(token_budget),
        timeout_seconds=int(timeout_seconds),
        approval_policy=(
            ApprovalPolicy.REQUIRED
            if action in RELAY_REVIEW_ACTIONS
            else ApprovalPolicy.NONE
        ),
    )
    envelope.validate()
    return envelope


def build_relay_review_canary_envelope(
    paper_id: str,
    *,
    model: str = "",
    writing_root: Path | None = None,
    staging_root: Path | None = None,
    token_budget: int = 50000,
    max_output_tokens: int = 8000,
    timeout_seconds: int = 600,
) -> TaskEnvelope:
    """Build a third-party relay task with a pinned, durably approved intent."""

    return build_review_canary_envelope(
        paper_id,
        executor=REVIEW_ROUTE_EXECUTORS[RELAY_REVIEW_ACTION],
        action=RELAY_REVIEW_ACTION,
        model=model,
        writing_root=writing_root,
        staging_root=staging_root,
        token_budget=token_budget,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def build_codex_cli_relay_review_canary_envelope(
    paper_id: str,
    *,
    model: str = "",
    codex_cli_path: Path | str | None = None,
    writing_root: Path | None = None,
    staging_root: Path | None = None,
    token_budget: int = 50000,
    max_output_tokens: int = 8000,
    timeout_seconds: int = 600,
) -> TaskEnvelope:
    """Build an approved Qilin task executed by an isolated official Codex CLI."""

    return build_review_canary_envelope(
        paper_id,
        executor=REVIEW_ROUTE_EXECUTORS[CODEX_CLI_RELAY_REVIEW_ACTION],
        action=CODEX_CLI_RELAY_REVIEW_ACTION,
        model=model,
        codex_cli_path=codex_cli_path,
        writing_root=writing_root,
        staging_root=staging_root,
        token_budget=token_budget,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def build_codex_sdk_profile_review_canary_envelope(
    paper_id: str,
    *,
    provider_config: RuntimeProviderConfig,
    writing_root: Path | None = None,
    staging_root: Path | None = None,
    token_budget: int = 50000,
    max_output_tokens: int = 8000,
    timeout_seconds: int = 600,
) -> TaskEnvelope:
    """Build a required-approval review bound to one user provider profile."""

    return build_review_canary_envelope(
        paper_id,
        executor=REVIEW_ROUTE_EXECUTORS[CODEX_SDK_PROFILE_REVIEW_ACTION],
        action=CODEX_SDK_PROFILE_REVIEW_ACTION,
        model=provider_config.model,
        codex_sdk_provider_config=provider_config,
        writing_root=writing_root,
        staging_root=staging_root,
        token_budget=token_budget,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def expected_review_idempotency_key(
    task: TaskEnvelope,
    *,
    staging_root: Path,
) -> str:
    roles = task.payload.get("input_roles")
    if not isinstance(roles, dict) or set(roles) != set(INPUT_ORDER):
        raise ReviewCanaryError(
            "REVIEW_CANARY_INPUT_ROLES_INVALID",
            "Review input role manifest is invalid.",
        )
    hashes: dict[str, str] = {}
    locations: dict[str, str] = {}
    for role in INPUT_ORDER:
        value = roles.get(role)
        if not isinstance(value, dict):
            raise ReviewCanaryError(
                "REVIEW_CANARY_INPUT_ROLES_INVALID",
                f"Review input role is invalid: {role}",
            )
        hashes[role] = str(value.get("sha256") or "")
        locations[role] = str(value.get("uri") or "")
    input_manifest_sha256 = _sha256(canonical_json(hashes).encode("utf-8"))
    if input_manifest_sha256 != str(task.payload.get("input_manifest_sha256") or ""):
        raise ReviewCanaryError(
            "REVIEW_CANARY_INPUT_MANIFEST_MISMATCH",
            "Review input manifest hash is invalid.",
        )
    if (
        task.payload.get("outer_adapter_version") != OUTER_ADAPTER_VERSION
        or task.payload.get("token_estimator_id") != TOKEN_ESTIMATOR_ID
        or task.payload.get("prompt_transform_id") != PROMPT_TRANSFORM_ID
    ):
        raise ReviewCanaryError(
            "REVIEW_CANARY_ADAPTER_VERSION_INVALID",
            "Review adapter contract version is invalid.",
        )
    schema = (
        "review_canary_codex_cli_relay_v1"
        if task.action == CODEX_CLI_RELAY_REVIEW_ACTION
        else "review_canary_codex_sdk_profile_v1"
        if task.action == CODEX_SDK_PROFILE_REVIEW_ACTION
        else "review_canary_relay_v1"
        if task.action == RELAY_REVIEW_ACTION
        else "review_canary_v1"
    )
    provider_keys = review_provider_payload_keys(task.action)
    payload = {
        "schema": schema,
        "action": task.action,
        "paper_id": task.paper_id,
        "executor": task.executor,
        "model": str(task.payload.get("model") or "") or "unconfigured",
        "input_manifest_sha256": input_manifest_sha256,
        "input_locations_sha256": _sha256(canonical_json(locations).encode("utf-8")),
        "rendered_prompt_sha256": str(task.payload.get("rendered_prompt_sha256") or ""),
        "outer_adapter_version": OUTER_ADAPTER_VERSION,
        "token_estimator_id": TOKEN_ESTIMATOR_ID,
        "token_budget": int(task.token_budget or 0),
        "max_output_tokens": int(task.payload.get("max_output_tokens") or 0),
        "timeout_seconds": int(task.timeout_seconds),
        "staging_root": str(Path(staging_root).resolve()),
        "provider_binding": {key: task.payload.get(key) for key in provider_keys},
    }
    return f"review-canary:{_sha256(canonical_json(payload).encode('utf-8'))}"


def validate_review_source_scope(task: TaskEnvelope, *, writing_root: Path) -> None:
    expected = _input_paths(task.paper_id, Path(writing_root))
    mapped = _artifact_map(task)
    for role in INPUT_ORDER:
        try:
            observed = Path(mapped[role].uri).resolve(strict=True)
            required = expected[role].resolve(strict=True)
        except OSError as exc:
            raise ReviewCanaryError(
                "REVIEW_CANARY_INPUT_UNREADABLE",
                f"Review input cannot be resolved: {role}",
            ) from exc
        if observed != required:
            raise ReviewCanaryError(
                "REVIEW_CANARY_SOURCE_SCOPE_DENIED",
                f"Review input is outside the configured paper role: {role}",
            )


def _artifact_map(task: TaskEnvelope) -> dict[str, ArtifactRef]:
    by_uri = {str(Path(item.uri).resolve()): item for item in task.inputs}
    roles = task.payload.get("input_roles", {})
    if not isinstance(roles, dict) or set(roles) != set(INPUT_ORDER):
        raise ReviewCanaryError("REVIEW_CANARY_INPUT_ROLES_INVALID", "Review input role manifest is invalid.")
    mapped: dict[str, ArtifactRef] = {}
    for role in INPUT_ORDER:
        value = roles.get(role)
        if not isinstance(value, dict):
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_ROLES_INVALID", f"Review input role is invalid: {role}")
        uri = str(Path(str(value.get("uri") or "")).resolve())
        artifact = by_uri.get(uri)
        if artifact is None or artifact.sha256 != str(value.get("sha256") or ""):
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_ROLES_INVALID", f"Review artifact mismatch: {role}")
        mapped[role] = artifact
    return mapped


def verify_review_inputs(task: TaskEnvelope) -> dict[str, bytes]:
    mapped = _artifact_map(task)
    raw: dict[str, bytes] = {}
    for role, artifact in mapped.items():
        path = Path(artifact.uri)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_UNREADABLE", f"Review input cannot be read: {role}") from exc
        if _sha256(payload) != artifact.sha256:
            raise ReviewCanaryError("REVIEW_CANARY_INPUT_SHA256_MISMATCH", f"Review input changed: {role}")
        raw[role] = payload
    return raw


def prepare_review_execution_task(task: TaskEnvelope) -> TaskEnvelope:
    if task.stage != "writing" or REVIEW_ROUTE_EXECUTORS.get(task.action) != task.executor:
        raise ReviewCanaryError("REVIEW_CANARY_ROUTE_INVALID", "Review canary route is not allowed.")
    if not task.network_access or task.allowed_tools or not task.payload.get("staging_only"):
        raise ReviewCanaryError("REVIEW_CANARY_CAPABILITY_INVALID", "Review canary capabilities are not staging-only.")
    raw = verify_review_inputs(task)
    source_prompt = raw["source_prompt"].decode("utf-8-sig")
    rendered_prompt = _render_prompt(source_prompt)
    if _sha256(raw["source_prompt"]) != str(task.payload.get("source_prompt_sha256") or ""):
        raise ReviewCanaryError("REVIEW_CANARY_PROMPT_SHA256_MISMATCH", "Source Prompt 07 hash changed.")
    if _sha256(rendered_prompt.encode("utf-8")) != str(task.payload.get("rendered_prompt_sha256") or ""):
        raise ReviewCanaryError("REVIEW_CANARY_RENDERED_PROMPT_MISMATCH", "Rendered Prompt 07 hash changed.")

    instructions = (
        "This is a staging-only review canary. Follow the immutable review task below. "
        "Do not browse, call tools, modify files, or claim that a filesystem write occurred. "
        "No rendered PDF pages or figure pixels are provided; distinguish TeX-source evidence from visual verification. "
        "Return only the Markdown body that belongs in review_round_1.md, beginning exactly with "
        "'# Review Round 1'. Do not wrap the answer in a code fence.\n\n"
        f"<immutable_review_prompt transform_id=\"{PROMPT_TRANSFORM_ID}\">\n"
        f"{rendered_prompt}\n</immutable_review_prompt>"
    )
    input_blocks = []
    for role in ("idea", "references", "literature_map", "novelty_check", "draft_v3"):
        text = raw[role].decode("utf-8-sig")
        input_blocks.append(f"<review_input role=\"{role}\">\n{text}\n</review_input>")
    transient = task.to_dict()
    prepared_input = "\n\n".join(input_blocks)
    if task.action == CODEX_SDK_PROFILE_REVIEW_ACTION:
        transient["payload"] = {
            "prompt": f"{instructions}\n\n{prepared_input}",
            "sandbox": "read_only",
            "model": str(task.payload.get("model") or ""),
        }
    else:
        transient["payload"] = {
            **task.payload,
            "_prepared_instructions": instructions,
            "_prepared_input": prepared_input,
            "_estimated_input_tokens": estimate_input_tokens(
                instructions,
                prepared_input,
            ),
        }
    return TaskEnvelope.from_dict(transient)


def estimate_input_tokens(instructions: str, input_text: str) -> int:
    """Return a conservative, tokenizer-independent preflight estimate.

    The exact server tokenizer remains authoritative and is checked again from
    response usage. Dividing UTF-8 bytes by three intentionally overestimates
    ordinary English/LaTeX while remaining deterministic in the EXE.
    """

    total_bytes = len(instructions.encode("utf-8")) + len(input_text.encode("utf-8"))
    return max(1, (total_bytes + 2) // 3)


def _section(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"(?ms)^{re.escape(heading)}\s*\n(.*?)(?=^##?\s|\Z)")
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _declared_total(section: str, label: str) -> int | None:
    normalized = section.replace("**", "").replace("__", "")
    label_pattern = r"(?:figures?|图(?:片)?)" if label == "figure" else r"(?:tables?|表(?:格)?)"
    patterns = (
        rf"(?im)^(?:\s*[-*]\s*)?(?:\*\*)?(?:current\s+)?{label_pattern}(?:\s*\*\*)?\s*(?:total|count|总数|数量)(?:\s*\*\*)?\s*[:：]\s*(\d+)\b",
        rf"(?im)^(?:\s*[-*]\s*)?(?:\*\*)?(?:total|number\s+of)\s+{label_pattern}(?:\s*\*\*)?\s*[:：]?\s*(\d+)\b",
        rf"(?im)^(?:\s*[-*]\s*)?{label_pattern}\s*[:：]\s*(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1))
    return None


def validate_review_markdown(
    markdown: str,
    *,
    expected_figure_count: int,
    expected_table_count: int,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    normalized = str(markdown or "").lstrip("\ufeff")
    if not normalized.startswith("# Review Round 1\n"):
        errors.append("REVIEW_HEADING_INVALID")
    if normalized.lstrip().startswith("```") or "```markdown" in normalized.lower():
        errors.append("REVIEW_CODE_FENCE_FORBIDDEN")

    positions: list[int] = []
    for heading in REQUIRED_HEADINGS:
        occurrences = len(re.findall(rf"(?m)^{re.escape(heading)}\s*$", normalized))
        if occurrences != 1:
            errors.append(f"REVIEW_HEADING_COUNT:{heading}")
            positions.append(-1)
        else:
            positions.append(normalized.find(heading))
    observed = [item for item in positions if item >= 0]
    if observed != sorted(observed):
        errors.append("REVIEW_HEADING_ORDER_INVALID")

    verdict_section = _section(normalized, "## Overall Verdict")
    verdict = next((line.strip(" *`\t") for line in verdict_section.splitlines() if line.strip()), "")
    if verdict not in ALLOWED_VERDICTS:
        errors.append("REVIEW_VERDICT_INVALID")

    expected_header = _markdown_cells(FIGURE_TABLE_HEADER)
    if not any(_markdown_cells(line) == expected_header for line in normalized.splitlines()):
        errors.append("REVIEW_FIGURE_TABLE_HEADER_MISSING")
    figure_rows = sorted(
        {
            int(match.group(1))
            for match in re.finditer(r"(?m)^\|\s*(?:\*\*)?(?:Fig\.\s*)?(\d+)(?:\*\*)?\s*\|", normalized)
        }
    )
    expected_figures = list(range(1, max(0, int(expected_figure_count)) + 1))
    if figure_rows != expected_figures:
        errors.append("REVIEW_FIGURE_ROWS_MISMATCH")

    figure_section = _section(normalized, "## Figure and Quantitative Evidence Review")
    figure_total = _declared_total(figure_section, "figure")
    table_total = _declared_total(figure_section, "table")
    if figure_total != int(expected_figure_count):
        errors.append("REVIEW_FIGURE_TOTAL_MISMATCH")
    if table_total != int(expected_table_count):
        errors.append("REVIEW_TABLE_TOTAL_MISMATCH")

    revisions = _section(normalized, "## Required Revisions")
    if not re.search(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+\S", revisions):
        errors.append("REVIEW_REQUIRED_CHECKLIST_MISSING")

    lower = normalized.lower()
    issue_families = {
        "reproducibility": bool(re.search(r"reproduc|seed|hardware|runtime|protocol|可复现|随机种子|硬件|运行时间|实验协议", lower)),
        "implementation": bool(re.search(r"implementation|pseudocode|controller|complexity|实现|伪代码|控制器|复杂度", lower)),
        "baseline_fairness": bool(re.search(r"baseline|fair comparison|tuning procedure|基线|公平比较|调参", lower)),
        "novelty_related_work": bool(re.search(r"novelty|related work|incremental|新颖|创新|相关工作|增量", lower)),
        "quantitative_evidence": bool(re.search(r"quantitative|data-driven|error bars|csv|plot|定量|数据驱动|误差棒|数据图", lower)),
    }
    if sum(issue_families.values()) < 4:
        errors.append("REVIEW_ISSUE_COVERAGE_INSUFFICIENT")
    if verdict not in {"Weak Reject", "Reject"}:
        warnings.append("FIXTURE_EXPECTED_STRICT_REJECTION")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "verdict": verdict,
        "figure_rows": figure_rows,
        "issue_families": issue_families,
    }
