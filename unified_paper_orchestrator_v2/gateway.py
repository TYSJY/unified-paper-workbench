from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from .config import DATABASE_PATH, STATE_DIR
from .contracts import (
    ApprovalPolicy,
    ArtifactRef,
    ResultStatus,
    TaskEnvelope,
    TaskResult,
    canonical_json,
)
from .executors import (
    CANONICAL_COMMIT_ACTION,
    CanonicalCommitExecutor,
    ExecutorRegistry,
    build_default_registry,
)
from .executors.codex_cli_relay import (
    CodexCLIRelayReviewExecutor,
)
from .executors.codex_sdk import CodexSDKExecutor
from .executors.legacy import LegacyCodexAutomationExecutor
from .executors.openai_relay import OpenAICompatibleRelayReviewExecutor
from .executors.openai_review import OpenAIReviewExecutor
from .kernel import WorkflowKernel
from .review_canary import (
    CODEX_CLI_RELAY_REVIEW_ACTION,
    CODEX_SDK_PROFILE_REVIEW_ACTION,
    DEFAULT_STAGING_ROOT,
    RELAY_REVIEW_ACTION,
    RELAY_REVIEW_ACTIONS,
    REVIEW_ACTION,
    REVIEW_ACTIONS,
    REVIEW_ROUTE_EXECUTORS,
    ReviewCanaryError,
    build_codex_cli_relay_review_canary_envelope,
    build_codex_sdk_profile_review_canary_envelope,
    build_relay_review_canary_envelope,
    build_review_canary_envelope,
    codex_cli_relay_static_provider_payload,
    codex_sdk_profile_provider_payload,
    expected_review_idempotency_key,
    prepare_review_execution_task,
    relay_review_provider_payload,
    review_payload_keys,
    validate_review_markdown,
    validate_review_source_scope,
    verify_review_inputs,
)
from .provider_runtime import RuntimeProviderConfig, provider_config_digest
from .staging import StagingError, StagingStore
from .store import EventStore


class GatewayError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_CANONICAL_PAYLOAD_KEYS = frozenset(
    {
        "commit_id",
        "root_name",
        "relative_path",
        "expected_exists",
        "expected_sha256",
    }
)
_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NON_LIVE_RESPONSE_ID = re.compile(
    r"(?i)^(?:offline(?:[_-]|$)|mock(?:[_-]|$)|fake(?:[_-]|$)|fixture(?:[_-]|$)|test(?:[_-]|$))"
)
_CODEX_THREAD_ID = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CODEX_CLI_ENVELOPE_RECEIPT_KEYS = (
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
_CODEX_CLI_RUNTIME_RECEIPT_KEYS = (
    "codex_cli_version",
    "billing_credential_fingerprint",
    "requested_model",
    "reported_model",
    "provider_response_id",
    "provider_external_ref_kind",
)
_AMBIGUOUS_REVIEW_RETRY_CODES = frozenset(
    {
        "GATEWAY_EXECUTOR_EXCEPTION",
        "OPENAI_API_REQUEST_FAILED",
        "OPENAI_API_RETRYABLE",
        "OPENAI_API_TIMEOUT",
        "OPENAI_RESPONSE_INCOMPLETE",
        "RELAY_API_REQUEST_FAILED",
        "RELAY_API_RETRYABLE",
        "RELAY_API_TIMEOUT",
        "RELAY_RESPONSE_INCOMPLETE",
    }
)


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


class ExecutionGateway:
    """Explicit-dispatch bridge from the V1 product shell to typed executors.

    The first release deliberately has no background worker. The canonical
    commit adapter is installed but remains disabled unless both construction-
    time writer authority and an isolated gateway route latch are supplied.
    """

    def __init__(
        self,
        *,
        database_path: Path = DATABASE_PATH,
        staging_root: Path = DEFAULT_STAGING_ROOT,
        registry: ExecutorRegistry | None = None,
        staging_store: StagingStore | None = None,
        allow_canonical_route: bool = False,
        review_writing_root: Path | None = None,
    ) -> None:
        self.store = EventStore(Path(database_path))
        self.registry = registry or build_default_registry()
        self.kernel = WorkflowKernel(self.store, self.registry)
        self.staging = staging_store or StagingStore(Path(staging_root))
        self._allow_canonical_route = bool(allow_canonical_route)
        if review_writing_root is None:
            from unified_paper_orchestrator.config import WRITING_ROOT

            review_writing_root = WRITING_ROOT
        self.review_writing_root = Path(review_writing_root).resolve()

    def submit(self, envelope: TaskEnvelope) -> dict[str, Any]:
        envelope = TaskEnvelope.from_dict(
            {**envelope.to_dict(), "dispatch_owner": "gateway"}
        )
        self._validate_policy(envelope)
        try:
            return self._attach_approval(self.kernel.create_task(envelope))
        except ValueError as exc:
            code = (
                "GATEWAY_IDEMPOTENCY_CONFLICT"
                if "idempotency key reused" in str(exc)
                else "GATEWAY_TASK_INVALID"
            )
            raise GatewayError(code, "Gateway task failed its durable envelope contract.") from exc

    def submit_review_canary(
        self,
        paper_id: str,
        *,
        model: str = "",
        writing_root: Path | None = None,
        token_budget: int = 50000,
        max_output_tokens: int = 8000,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        selected_writing_root = (
            Path(writing_root).resolve()
            if writing_root is not None
            else self.review_writing_root
        )
        if selected_writing_root != self.review_writing_root:
            raise GatewayError(
                "GATEWAY_REVIEW_WRITING_ROOT_DENIED",
                "Review canary writing root does not match the configured trusted root.",
            )
        envelope = build_review_canary_envelope(
            paper_id,
            model=model,
            writing_root=selected_writing_root,
            staging_root=self.staging.root,
            token_budget=token_budget,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return self.submit(envelope)

    def submit_relay_review_canary(
        self,
        paper_id: str,
        *,
        model: str = "",
        writing_root: Path | None = None,
        token_budget: int = 50000,
        max_output_tokens: int = 8000,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        selected_writing_root = (
            Path(writing_root).resolve()
            if writing_root is not None
            else self.review_writing_root
        )
        if selected_writing_root != self.review_writing_root:
            raise GatewayError(
                "GATEWAY_REVIEW_WRITING_ROOT_DENIED",
                "Relay review canary writing root does not match the configured trusted root.",
            )
        envelope = build_relay_review_canary_envelope(
            paper_id,
            model=model,
            writing_root=selected_writing_root,
            staging_root=self.staging.root,
            token_budget=token_budget,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return self.submit(envelope)

    def submit_codex_cli_relay_review_canary(
        self,
        paper_id: str,
        *,
        model: str = "",
        codex_cli_path: Path | str | None = None,
        writing_root: Path | None = None,
        token_budget: int = 50000,
        max_output_tokens: int = 8000,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        selected_writing_root = (
            Path(writing_root).resolve()
            if writing_root is not None
            else self.review_writing_root
        )
        if selected_writing_root != self.review_writing_root:
            raise GatewayError(
                "GATEWAY_REVIEW_WRITING_ROOT_DENIED",
                "Codex CLI relay canary writing root does not match the configured trusted root.",
            )
        envelope = build_codex_cli_relay_review_canary_envelope(
            paper_id,
            model=model,
            codex_cli_path=codex_cli_path,
            writing_root=selected_writing_root,
            staging_root=self.staging.root,
            token_budget=token_budget,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return self.submit(envelope)

    def submit_codex_sdk_profile_review_canary(
        self,
        paper_id: str,
        *,
        provider_config: RuntimeProviderConfig,
        writing_root: Path | None = None,
        token_budget: int = 50000,
        max_output_tokens: int = 8000,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        selected_writing_root = (
            Path(writing_root).resolve()
            if writing_root is not None
            else self.review_writing_root
        )
        if selected_writing_root != self.review_writing_root:
            raise GatewayError(
                "GATEWAY_REVIEW_WRITING_ROOT_DENIED",
                "Codex SDK review root does not match the configured trusted root.",
            )
        executor = self.registry.get("codex_sdk")
        if type(executor) is not CodexSDKExecutor or executor.provider_config is None:
            raise GatewayError(
                "GATEWAY_CODEX_SDK_PROFILE_NOT_CONFIGURED",
                "The gateway Codex SDK executor has no trusted provider profile.",
            )
        if provider_config_digest(executor.provider_config) != provider_config_digest(
            provider_config
        ):
            raise GatewayError(
                "GATEWAY_CODEX_SDK_PROFILE_MISMATCH",
                "The requested provider profile does not match the installed executor.",
            )
        envelope = build_codex_sdk_profile_review_canary_envelope(
            paper_id,
            provider_config=provider_config,
            writing_root=selected_writing_root,
            staging_root=self.staging.root,
            token_budget=token_budget,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        return self.submit(envelope)

    def submit_legacy_automation(
        self,
        operation: str,
        target_stage: str,
        *,
        operation_id: str = "",
    ) -> dict[str, Any]:
        normalized_operation = str(operation or "").strip().lower()
        if normalized_operation not in {"status", "start", "pause"}:
            raise GatewayError("GATEWAY_LEGACY_OPERATION_INVALID", "Legacy Automation operation is invalid.")
        normalized_stage = str(target_stage or "").strip()
        if normalized_stage not in {"writing", "figures", "journal_author", "submission"}:
            raise GatewayError("GATEWAY_LEGACY_STAGE_INVALID", "Legacy Automation target stage is invalid.")
        action = f"legacy.automation.{normalized_operation}"
        side_effecting = normalized_operation in {"start", "pause"}
        normalized_id = str(operation_id or "").strip()
        if side_effecting and not normalized_id:
            raise GatewayError(
                "GATEWAY_OPERATION_ID_REQUIRED",
                "An explicit operation_id is required for Automation start/pause.",
            )
        envelope = TaskEnvelope(
            paper_id="system",
            stage="system",
            action=action,
            executor="legacy_codex",
            idempotency_key=(
                f"legacy-automation:{normalized_operation}:{normalized_stage}:{normalized_id}"
                if side_effecting
                else ""
            ),
            payload={"target_stage": normalized_stage, "operation_id": normalized_id},
            network_access=False,
            allowed_tools=[],
            writable_roots=[],
            approval_policy=ApprovalPolicy.REQUIRED if side_effecting else ApprovalPolicy.NONE,
        )
        return self.submit(envelope)

    def submit_canonical_commit(
        self,
        *,
        paper_id: str,
        commit_id: str,
        staged: ArtifactRef,
        root_name: str,
        relative_path: str,
        expected_exists: bool,
        expected_sha256: str = "",
    ) -> dict[str, Any]:
        executor = self._canonical_executor(require_enabled=True)
        try:
            writable_root = executor.configured_root(root_name)
        except Exception as exc:
            raise GatewayError(
                "GATEWAY_CANONICAL_ROOT_UNKNOWN",
                "Canonical root is not configured for this gateway.",
            ) from exc
        envelope = TaskEnvelope(
            paper_id=str(paper_id or "").strip(),
            stage="system",
            action=CANONICAL_COMMIT_ACTION,
            executor=executor.name,
            idempotency_key=f"canonical-commit:{str(commit_id or '').strip()}",
            inputs=[staged],
            payload={
                "commit_id": str(commit_id or "").strip(),
                "root_name": str(root_name or "").strip(),
                "relative_path": str(relative_path or ""),
                "expected_exists": expected_exists,
                "expected_sha256": str(expected_sha256 or "").strip().lower(),
            },
            network_access=False,
            allowed_tools=[],
            writable_roots=[str(writable_root)],
            approval_policy=ApprovalPolicy.REQUIRED,
        )
        return self.submit(envelope)

    def run_once(self, task_id: str = "") -> dict[str, Any] | None:
        envelope = self.store.claim_task(task_id, dispatch_owner="gateway")
        if envelope is None:
            if task_id:
                record = self.task(task_id)
                if record.get("dispatch_owner") != "gateway":
                    raise GatewayError(
                        "GATEWAY_DISPATCH_OWNER_MISMATCH",
                        "Task belongs to the direct kernel dispatcher, not the execution gateway.",
                    )
                raise GatewayError(
                    "GATEWAY_TASK_NOT_QUEUED",
                    f"Gateway task is not queued: status={record.get('status', 'unknown')}",
                )
            return None
        executor_result: TaskResult | None = None
        try:
            self._validate_policy(envelope)
            self._verify_approved_intent(envelope)
            execution_attempt_token_sha256 = ""
            if envelope.action in REVIEW_ACTIONS:
                try:
                    execution_attempt_token_sha256 = (
                        self.store.active_execution_attempt_token_sha256(
                            envelope.task_id,
                            dispatch_owner="gateway",
                        )
                    )
                except (KeyError, ValueError) as exc:
                    raise GatewayError(
                        "GATEWAY_EXECUTION_ATTEMPT_BINDING_MISSING",
                        "Review execution has no active ledger claim binding.",
                    ) from exc
            # A normally queued dispatch never consumes a pre-existing staging
            # directory. Crash recovery is a separate, explicit running-task
            # operation below, so planted receipts cannot bypass the API call.
            execution_envelope = (
                prepare_review_execution_task(envelope)
                if envelope.action in REVIEW_ACTIONS
                else envelope
            )
            if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
                self.staging.ensure_root()
            executor = self.registry.get(execution_envelope.executor)
            executor_result = executor.execute(execution_envelope)
            result = executor_result
            if (
                envelope.action in REVIEW_ACTIONS
                and executor_result.status is ResultStatus.COMPLETED
            ):
                result = self._finalize_review(
                    envelope,
                    executor_result,
                    execution_attempt_token_sha256=execution_attempt_token_sha256,
                )
        except ReviewCanaryError as exc:
            result = self._compact_failure(envelope, executor_result, ResultStatus.FATAL_FAILURE, exc.code, str(exc))
        except StagingError as exc:
            result = self._compact_failure(envelope, executor_result, ResultStatus.FATAL_FAILURE, exc.code, str(exc))
        except GatewayError as exc:
            result = self._compact_failure(envelope, executor_result, ResultStatus.FATAL_FAILURE, exc.code, str(exc))
        except Exception as exc:
            result = self._compact_failure(
                envelope,
                executor_result,
                ResultStatus.RETRYABLE_FAILURE,
                "GATEWAY_EXECUTOR_EXCEPTION",
                f"Execution gateway failed with {type(exc).__name__}.",
                extra_data={"error_type": type(exc).__name__},
            )
        return self.store.complete_task(envelope.task_id, result, dispatch_owner="gateway")

    def task(self, task_id: str) -> dict[str, Any]:
        try:
            return self._attach_approval(self.store.get_task(str(task_id or "")))
        except KeyError as exc:
            raise GatewayError("GATEWAY_TASK_NOT_FOUND", "Gateway task does not exist.") from exc

    def approval(self, task_id: str) -> dict[str, Any]:
        self.task(task_id)
        try:
            return self.store.get_approval(task_id)
        except KeyError as exc:
            raise GatewayError(
                "GATEWAY_APPROVAL_NOT_FOUND",
                "Gateway task does not have an approval request.",
            ) from exc

    def retry(
        self,
        task_id: str,
        *,
        note: str = "explicit CLI retry",
        acknowledge_possible_duplicate_charge: bool = False,
    ) -> dict[str, Any]:
        record = self.task(task_id)
        envelope = record.get("envelope") if isinstance(record.get("envelope"), dict) else {}
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        blocker_code = str(result.get("blocker_code") or "")
        ambiguous_review_result = (
            envelope.get("action") in REVIEW_ACTIONS
            and blocker_code in _AMBIGUOUS_REVIEW_RETRY_CODES
        )
        if ambiguous_review_result and not acknowledge_possible_duplicate_charge:
            raise GatewayError(
                "GATEWAY_REVIEW_RETRY_ACK_REQUIRED",
                "The prior API outcome may have incurred a charge; explicitly acknowledge a possible duplicate charge before requeueing.",
            )
        try:
            return self.store.requeue_task(
                task_id,
                note=note,
                dispatch_owner="gateway",
                acknowledgements={
                    "possible_duplicate_charge": bool(
                        ambiguous_review_result
                        and acknowledge_possible_duplicate_charge
                    )
                },
            )
        except ValueError as exc:
            raise GatewayError(
                "GATEWAY_TASK_NOT_RETRYABLE",
                "Only blocked, retryable_failure, or waiting_human tasks can be requeued.",
            ) from exc

    def approve(
        self,
        task_id: str,
        *,
        note: str = "explicit gateway approval",
        expected_intent_sha256: str = "",
    ) -> dict[str, Any]:
        approval = self.approval(task_id)
        expected = str(expected_intent_sha256 or "").strip().lower()
        actual = str(approval["payload"].get("intent_sha256") or "").strip().lower()
        if not expected:
            raise GatewayError(
                "GATEWAY_APPROVAL_INTENT_REQUIRED",
                "Gateway approval requires the displayed intent SHA256.",
            )
        if expected != actual:
            raise GatewayError(
                "GATEWAY_APPROVAL_INTENT_MISMATCH",
                "Gateway approval intent SHA256 does not match.",
            )
        try:
            return self.store.decide_approval(
                task_id,
                approve=True,
                note=note,
                dispatch_owner="gateway",
            )
        except (KeyError, ValueError) as exc:
            raise GatewayError("GATEWAY_TASK_NOT_APPROVABLE", "Gateway task cannot be approved.") from exc

    def recover_canonical_task(
        self,
        task_id: str,
        *,
        expected_intent_sha256: str,
    ) -> dict[str, Any]:
        """Reconcile a running canonical task after a process/store crash window."""

        record = self.task(task_id)
        if record.get("status") == "completed":
            return record
        if record.get("dispatch_owner") != "gateway" or record["envelope"].get(
            "action"
        ) != CANONICAL_COMMIT_ACTION:
            raise GatewayError(
                "GATEWAY_CANONICAL_RECOVERY_DENIED",
                "Only gateway-owned canonical commit tasks can be reconciled.",
            )
        if record.get("status") != "running":
            raise GatewayError(
                "GATEWAY_CANONICAL_RECOVERY_STATE_INVALID",
                "Canonical reconciliation requires a task left in running state.",
            )
        approval = self.approval(task_id)
        expected = str(expected_intent_sha256 or "").strip().lower()
        actual = str(approval["payload"].get("intent_sha256") or "").strip().lower()
        if not expected or expected != actual:
            raise GatewayError(
                "GATEWAY_APPROVAL_INTENT_MISMATCH",
                "Canonical reconciliation requires the original approved intent SHA256.",
            )
        envelope = TaskEnvelope.from_dict(record["envelope"])
        self._validate_canonical_policy(envelope, allow_missing_staged=True)
        self._verify_approved_intent(envelope)
        executor = self._canonical_executor(require_enabled=True)
        result = executor.reconcile(envelope)
        if result is None:
            result = executor.execute(envelope)
        try:
            return self.store.complete_task(task_id, result, dispatch_owner="gateway")
        except ValueError as exc:
            current = self.task(task_id)
            if current.get("status") != "running":
                return current
            raise GatewayError(
                "GATEWAY_CANONICAL_RECONCILIATION_FAILED",
                "Canonical audit could not be reconciled into the task ledger.",
            ) from exc

    def recover_review_task(
        self,
        task_id: str,
        *,
        expected_input_manifest_sha256: str,
    ) -> dict[str, Any]:
        """Complete a running review task only from a verified staged receipt."""

        record = self.task(task_id)
        if record.get("status") == "completed":
            return record
        envelope_data = record.get("envelope") if isinstance(record.get("envelope"), dict) else {}
        if (
            record.get("dispatch_owner") != "gateway"
            or envelope_data.get("action") not in REVIEW_ACTIONS
        ):
            raise GatewayError(
                "GATEWAY_REVIEW_RECOVERY_DENIED",
                "Only gateway-owned review canary tasks can be reconciled.",
            )
        if record.get("status") != "running":
            raise GatewayError(
                "GATEWAY_REVIEW_RECOVERY_STATE_INVALID",
                "Review reconciliation requires a task left in running state.",
            )
        envelope = TaskEnvelope.from_dict(envelope_data)
        expected = str(expected_input_manifest_sha256 or "").strip().lower()
        actual = str(envelope.payload.get("input_manifest_sha256") or "").strip().lower()
        if not expected or expected != actual:
            raise GatewayError(
                "GATEWAY_REVIEW_RECOVERY_INTENT_MISMATCH",
                "Review reconciliation requires the original input manifest SHA256.",
            )
        self._validate_policy(envelope)
        if not self.staging.review_exists(envelope.run_id, envelope.task_id):
            raise GatewayError(
                "GATEWAY_REVIEW_RECOVERY_EVIDENCE_MISSING",
                "No complete staged review receipt exists; the API call will not be repeated.",
            )
        try:
            execution_attempt_token_sha256 = (
                self.store.active_execution_attempt_token_sha256(
                    envelope.task_id,
                    dispatch_owner="gateway",
                )
            )
        except (KeyError, ValueError) as exc:
            raise GatewayError(
                "GATEWAY_REVIEW_RECOVERY_ATTEMPT_MISSING",
                "Review recovery requires the original active execution claim.",
            ) from exc
        result = self._review_result_from_staging(
            envelope,
            execution_attempt_token_sha256=execution_attempt_token_sha256,
        )
        try:
            return self.store.complete_task(task_id, result, dispatch_owner="gateway")
        except ValueError as exc:
            current = self.task(task_id)
            if current.get("status") != "running":
                return current
            raise GatewayError(
                "GATEWAY_REVIEW_RECONCILIATION_FAILED",
                "Verified staging receipt could not be reconciled into the task ledger.",
            ) from exc

    def recover_legacy_automation_task(
        self,
        task_id: str,
        *,
        expected_intent_sha256: str = "",
    ) -> dict[str, Any]:
        """Reconcile a running Automation task by read-only desired-state proof."""

        record = self.task(task_id)
        if record.get("status") == "completed":
            return record
        envelope_data = record.get("envelope") if isinstance(record.get("envelope"), dict) else {}
        action = str(envelope_data.get("action") or "")
        if record.get("dispatch_owner") != "gateway" or action not in {
            "legacy.automation.status",
            "legacy.automation.start",
            "legacy.automation.pause",
        }:
            raise GatewayError(
                "GATEWAY_LEGACY_RECOVERY_DENIED",
                "Only gateway-owned Automation tasks can be reconciled.",
            )
        if record.get("status") != "running":
            raise GatewayError(
                "GATEWAY_LEGACY_RECOVERY_STATE_INVALID",
                "Automation reconciliation requires a task left in running state.",
            )
        envelope = TaskEnvelope.from_dict(envelope_data)
        self._validate_policy(envelope)
        if action in {"legacy.automation.start", "legacy.automation.pause"}:
            approval = self.approval(task_id)
            expected = str(expected_intent_sha256 or "").strip().lower()
            actual = str(approval["payload"].get("intent_sha256") or "").strip().lower()
            if not expected or expected != actual or approval.get("status") != "approved":
                raise GatewayError(
                    "GATEWAY_APPROVAL_INTENT_MISMATCH",
                    "Automation reconciliation requires the original approved intent SHA256.",
                )
            self._verify_approved_intent(envelope)
        result = self._legacy_automation_executor().reconcile(envelope)
        try:
            return self.store.complete_task(task_id, result, dispatch_owner="gateway")
        except ValueError as exc:
            current = self.task(task_id)
            if current.get("status") != "running":
                return current
            raise GatewayError(
                "GATEWAY_LEGACY_RECONCILIATION_FAILED",
                "Automation status proof could not be reconciled into the task ledger.",
            ) from exc

    def status(self) -> dict[str, Any]:
        canonical = self._canonical_status()
        return {
            "schema_version": "1",
            "mode": "explicit_dispatch_only",
            "dispatch_owner": "gateway",
            "database_path": str(self.store.path),
            "staging_root": str(self.staging.root),
            "canonical_writes_enabled": False,
            "canonical_write_owner": "v1_existing_workflows",
            "canonical_write_switch": "backend_router_legacy_only",
            **canonical,
            "automatic_retry_enabled": False,
            "automatic_executor_fallback_enabled": False,
            "identity_surfaces": {
                "codex_desktop_chatgpt": {
                    "role": "external_development_control_plane",
                    "used_by_exe_execution": False,
                    "config_or_auth_read_by_gateway": False,
                },
                "official_openai_api": {
                    "role": "optional_separate_api_key_executor",
                    "shares_chatgpt_subscription": False,
                },
                "generic_qilin_api": {
                    "role": "separate_responses_compatible_executor",
                    "shares_codex_cli_identity": False,
                },
                "qilin_codex_cli": {
                    "role": "isolated_pinned_native_cli_executor",
                    "shares_desktop_config_or_auth": False,
                },
            },
            "review_source_policy": "fixed_role_paths_under_configured_writing_root",
            "review_provider_routes": [
                {
                    "action": "writing.review_round_1.canary",
                    "executor": "openai_review",
                    "provider_kind": "official_openai",
                    "provider_profile": "official_openai_responses_v1",
                    "provider_assurance": "official_endpoint_contract",
                    "approval_required": False,
                    "route_role": "optional_separate_official_api_key",
                    "cross_route_fallback": False,
                },
                {
                    "action": RELAY_REVIEW_ACTION,
                    "executor": REVIEW_ROUTE_EXECUTORS[RELAY_REVIEW_ACTION],
                    **relay_review_provider_payload(),
                    "approval_required": True,
                    "route_role": "generic_relay_compatibility_route",
                    "cross_route_fallback": False,
                },
                {
                    "action": CODEX_CLI_RELAY_REVIEW_ACTION,
                    "executor": REVIEW_ROUTE_EXECUTORS[
                        CODEX_CLI_RELAY_REVIEW_ACTION
                    ],
                    **codex_cli_relay_static_provider_payload(),
                    "approval_required": True,
                    "desktop_codex_identity_used": False,
                    "route_role": "preferred_qilin_codex_client_route",
                    "cross_route_fallback": False,
                },
            ],
            "review_transport_policies": [
                "direct_no_env_proxy",
                "explicit_loopback_proxy_no_env",
            ],
            "codex_cli_transport_policies": [
                "pinned_native_codex_cli_direct_no_env_proxy",
                "pinned_native_codex_cli_explicit_loopback_proxy_no_env",
            ],
            "generic_proxy_environment_ignored": True,
            "explicit_proxy_scope": "credential_free_http_loopback_only",
            "explicit_receipt_recovery": [
                *sorted(REVIEW_ACTIONS),
                "legacy.automation.status",
                "legacy.automation.start",
                "legacy.automation.pause",
                CANONICAL_COMMIT_ACTION,
            ],
            "runtime": self.store.summary(),
            "executors": self.registry.describe(),
            "supported_canaries": sorted(REVIEW_ACTIONS),
        }

    def _validate_policy(self, envelope: TaskEnvelope) -> None:
        if envelope.action == CANONICAL_COMMIT_ACTION:
            self._validate_canonical_policy(envelope)
            return
        if envelope.action in REVIEW_ACTIONS:
            expected_executor = REVIEW_ROUTE_EXECUTORS[envelope.action]
            if envelope.stage != "writing" or envelope.executor != expected_executor:
                raise GatewayError(
                    "GATEWAY_REVIEW_ROUTE_DENIED",
                    "Review canary action and executor do not match an enabled exact route pair.",
                )
            candidate = self.registry.get(expected_executor)
            expected_type = {
                REVIEW_ACTION: OpenAIReviewExecutor,
                RELAY_REVIEW_ACTION: OpenAICompatibleRelayReviewExecutor,
                CODEX_CLI_RELAY_REVIEW_ACTION: CodexCLIRelayReviewExecutor,
                CODEX_SDK_PROFILE_REVIEW_ACTION: CodexSDKExecutor,
            }[envelope.action]
            if type(candidate) is not expected_type:
                raise GatewayError(
                    "GATEWAY_REVIEW_EXECUTOR_INVALID",
                    "Review route is not backed by its trusted typed executor adapter.",
                )
            expected_payload_keys = review_payload_keys(envelope.action)
            if set(envelope.payload) != expected_payload_keys:
                raise GatewayError(
                    "GATEWAY_REVIEW_CONTRACT_INVALID",
                    "Review canary payload does not match the typed action contract.",
                )
            if envelope.action in RELAY_REVIEW_ACTIONS:
                if envelope.approval_policy is not ApprovalPolicy.REQUIRED:
                    raise GatewayError(
                        "GATEWAY_RELAY_APPROVAL_REQUIRED",
                        "Third-party relay review requires durable approval_policy=required.",
                    )
                expected_provider = (
                    codex_cli_relay_static_provider_payload()
                    if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION
                    else codex_sdk_profile_provider_payload(
                        self._codex_sdk_executor().provider_config
                    )
                    if envelope.action == CODEX_SDK_PROFILE_REVIEW_ACTION
                    else relay_review_provider_payload()
                )
                if any(
                    envelope.payload.get(key) != value
                    for key, value in expected_provider.items()
                ):
                    raise GatewayError(
                        "GATEWAY_RELAY_ENDPOINT_BINDING_INVALID",
                        "Relay task does not match the pinned third-party endpoint and disclosure contract.",
                    )
                if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
                    if (
                        _SHA256.fullmatch(
                            str(envelope.payload.get("codex_cli_sha256") or "")
                        )
                        is None
                        or _SHA256.fullmatch(
                            str(envelope.payload.get("relay_key_fingerprint") or "")
                        )
                        is None
                        or not Path(
                            str(envelope.payload.get("codex_cli_path") or "")
                        ).is_absolute()
                    ):
                        raise GatewayError(
                            "GATEWAY_CODEX_CLI_IDENTITY_BINDING_INVALID",
                            "Codex CLI relay task is missing its approved binary or credential identity binding.",
                        )
            elif envelope.approval_policy is not ApprovalPolicy.NONE:
                raise GatewayError(
                    "GATEWAY_REVIEW_APPROVAL_POLICY_INVALID",
                    "Official API review does not use the third-party relay approval contract.",
                )
            if not envelope.network_access or envelope.allowed_tools:
                raise GatewayError(
                    "GATEWAY_REVIEW_CAPABILITY_DENIED",
                    "Review canary permits one API request and no model tools.",
                )
            if not envelope.payload.get("staging_only"):
                raise GatewayError("GATEWAY_CANONICAL_WRITE_DENIED", "Review canary must be staging-only.")
            validate_review_source_scope(
                envelope,
                writing_root=self.review_writing_root,
            )
            expected_key = expected_review_idempotency_key(
                envelope,
                staging_root=self.staging.root,
            )
            if envelope.idempotency_key != expected_key:
                raise GatewayError(
                    "GATEWAY_REVIEW_IDEMPOTENCY_INVALID",
                    "Review canary idempotency key is not bound to its full typed request.",
                )
            self.staging.task_dir(envelope.run_id, envelope.task_id)
            roots = [str(Path(item).resolve()) for item in envelope.writable_roots]
            if roots != [str(self.staging.root)]:
                raise GatewayError(
                    "GATEWAY_WRITABLE_ROOT_DENIED",
                    "Review canary writable root must be the private staging directory only.",
                )
            return
        if envelope.stage == "system" and envelope.action == "legacy.apply":
            raise GatewayError(
                "GATEWAY_LEGACY_APPLY_DISABLED",
                "0.4.4 does not route legacy production writes through the execution gateway.",
            )
        if (
            envelope.stage == "system"
            and envelope.action == "legacy.plan"
            and envelope.executor in {"legacy", "legacy_codex"}
        ):
            return
        if envelope.stage == "system" and envelope.action in {
            "legacy.automation.status",
            "legacy.automation.start",
            "legacy.automation.pause",
        }:
            self._legacy_automation_executor()
            if envelope.executor != "legacy_codex":
                raise GatewayError(
                    "GATEWAY_LEGACY_ROUTE_DENIED",
                    "Legacy Automation actions must use the legacy_codex executor.",
                )
            if set(envelope.payload) != {"target_stage", "operation_id"}:
                raise GatewayError(
                    "GATEWAY_LEGACY_CONTRACT_INVALID",
                    "Legacy Automation payload does not match the typed action contract.",
                )
            target_stage = str(envelope.payload.get("target_stage") or "")
            operation_id = str(envelope.payload.get("operation_id") or "")
            if target_stage not in {"writing", "figures", "journal_author", "submission"}:
                raise GatewayError(
                    "GATEWAY_LEGACY_STAGE_INVALID",
                    "Legacy Automation target stage is invalid.",
                )
            if envelope.network_access or envelope.allowed_tools or envelope.writable_roots:
                raise GatewayError(
                    "GATEWAY_LEGACY_CAPABILITY_DENIED",
                    "Legacy Automation actions require zero network, tools, and writable roots.",
                )
            if (
                envelope.action in {"legacy.automation.start", "legacy.automation.pause"}
                and envelope.approval_policy is not ApprovalPolicy.REQUIRED
            ):
                raise GatewayError(
                    "GATEWAY_LEGACY_APPROVAL_REQUIRED",
                    "Legacy Automation start/pause requires explicit approval_policy=required.",
                )
            if envelope.action in {"legacy.automation.start", "legacy.automation.pause"}:
                operation = envelope.action.rsplit(".", 1)[-1]
                expected_key = (
                    f"legacy-automation:{operation}:{target_stage}:{operation_id}"
                )
                if (
                    _SAFE_OPERATION_ID.fullmatch(operation_id) is None
                    or envelope.idempotency_key != expected_key
                ):
                    raise GatewayError(
                        "GATEWAY_LEGACY_IDEMPOTENCY_INVALID",
                        "Automation control requires an explicit operation ID bound to its request.",
                    )
            elif operation_id:
                raise GatewayError(
                    "GATEWAY_LEGACY_CONTRACT_INVALID",
                    "Read-only Automation status cannot include an operation ID.",
                )
            return
        raise GatewayError(
            "GATEWAY_ACTION_DENIED",
            f"Action is not enabled by the first multi-executor gateway release: {envelope.action}",
        )

    def _validate_canonical_policy(
        self,
        envelope: TaskEnvelope,
        *,
        allow_missing_staged: bool = False,
    ) -> None:
        if envelope.stage != "system" or envelope.executor != "canonical_commit":
            raise GatewayError(
                "GATEWAY_CANONICAL_ROUTE_DENIED",
                "Canonical commit must use the system canonical_commit route.",
            )
        executor = self._canonical_executor(require_enabled=True)
        if envelope.network_access or envelope.allowed_tools:
            raise GatewayError(
                "GATEWAY_CANONICAL_CAPABILITY_DENIED",
                "Canonical commit requires zero network access and zero external tools.",
            )
        if envelope.approval_policy is not ApprovalPolicy.REQUIRED:
            raise GatewayError(
                "GATEWAY_CANONICAL_APPROVAL_REQUIRED",
                "Canonical commit requires durable approval before execution.",
            )
        if set(envelope.payload) != _CANONICAL_PAYLOAD_KEYS:
            raise GatewayError(
                "GATEWAY_CANONICAL_CONTRACT_INVALID",
                "Canonical commit payload does not match the strict contract.",
            )
        commit_id = str(envelope.payload.get("commit_id") or "")
        if _SAFE_OPERATION_ID.fullmatch(commit_id) is None:
            raise GatewayError(
                "GATEWAY_CANONICAL_COMMIT_ID_INVALID",
                "Canonical commit_id is invalid.",
            )
        if envelope.idempotency_key != f"canonical-commit:{commit_id}":
            raise GatewayError(
                "GATEWAY_CANONICAL_IDEMPOTENCY_INVALID",
                "Canonical idempotency key is not bound to commit_id.",
            )
        if not isinstance(envelope.payload.get("expected_exists"), bool):
            raise GatewayError(
                "GATEWAY_CANONICAL_CAS_INVALID",
                "Canonical destination existence must be an explicit boolean.",
            )
        expected_sha256 = str(envelope.payload.get("expected_sha256") or "").lower()
        if bool(envelope.payload["expected_exists"]) != bool(expected_sha256) or (
            expected_sha256 and _SHA256.fullmatch(expected_sha256) is None
        ):
            raise GatewayError(
                "GATEWAY_CANONICAL_CAS_INVALID",
                "Canonical destination CAS hash is invalid.",
            )
        if len(envelope.inputs) != 1 or _SHA256.fullmatch(
            str(envelope.inputs[0].sha256 or "").lower()
        ) is None:
            raise GatewayError(
                "GATEWAY_CANONICAL_INPUT_INVALID",
                "Canonical commit requires exactly one staged artifact with SHA256.",
            )
        self._validate_staged_source(
            envelope.inputs[0],
            allow_missing=allow_missing_staged,
        )
        root_name = str(envelope.payload.get("root_name") or "")
        try:
            expected_root = executor.configured_root(root_name)
        except Exception as exc:
            raise GatewayError(
                "GATEWAY_CANONICAL_ROOT_UNKNOWN",
                "Canonical root is not configured for this gateway.",
            ) from exc
        if len(envelope.writable_roots) != 1 or self._normalized_path(
            envelope.writable_roots[0]
        ) != self._normalized_path(expected_root):
            raise GatewayError(
                "GATEWAY_CANONICAL_WRITABLE_ROOT_DENIED",
                "Canonical writable root must exactly match its authorized root.",
            )

    def _verify_approved_intent(self, envelope: TaskEnvelope) -> None:
        if envelope.approval_policy is not ApprovalPolicy.REQUIRED:
            return
        try:
            approval = self.store.get_approval(envelope.task_id)
        except KeyError as exc:
            raise GatewayError(
                "GATEWAY_APPROVAL_MISSING",
                "Approved side-effecting task has no durable approval record.",
            ) from exc
        if approval.get("status") != "approved":
            raise GatewayError(
                "GATEWAY_APPROVAL_NOT_APPROVED",
                "Side-effecting task approval is not in approved state.",
            )
        actual = str(approval["payload"].get("intent_sha256") or "").strip().lower()
        expected = self._intent_sha256(envelope)
        if actual != expected:
            raise GatewayError(
                "GATEWAY_APPROVAL_INTENT_DRIFT",
                "Approved task intent no longer matches the durable task envelope.",
            )

    def _attach_approval(self, record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        envelope_value = record.get("envelope")
        envelope = envelope_value if isinstance(envelope_value, dict) else {}
        payload_value = envelope.get("payload")
        payload = payload_value if isinstance(payload_value, dict) else {}
        action = str(envelope.get("action") or "")
        if action in REVIEW_ACTIONS:
            result["recovery_intent"] = {
                "input_manifest_sha256": payload.get("input_manifest_sha256", ""),
                "staging_only": True,
            }
        try:
            approval = self.store.get_approval(str(record.get("task_id") or ""))
        except KeyError:
            return result
        if action.startswith("legacy.automation."):
            intent_summary = {
                "action": action,
                "target_stage": payload.get("target_stage", ""),
                "operation_id": payload.get("operation_id", ""),
            }
        elif action == CANONICAL_COMMIT_ACTION:
            intent_summary = {
                "action": action,
                "commit_id": payload.get("commit_id", ""),
                "root_name": payload.get("root_name", ""),
                "relative_path": payload.get("relative_path", ""),
                "expected_exists": payload.get("expected_exists"),
                "expected_sha256": payload.get("expected_sha256", ""),
            }
        elif action in RELAY_REVIEW_ACTIONS:
            intent_summary = {
                "action": action,
                "provider_kind": payload.get("provider_kind", ""),
                "provider_profile": payload.get("provider_profile", ""),
                "provider_assurance": payload.get("provider_assurance", ""),
                "wire_api": payload.get("wire_api", ""),
                "endpoint_base_url": payload.get("endpoint_base_url", ""),
                "endpoint_config_sha256": payload.get("endpoint_config_sha256", ""),
                "model": payload.get("model", ""),
                "input_manifest_sha256": payload.get("input_manifest_sha256", ""),
                "third_party_data_disclosure_ack": payload.get(
                    "third_party_data_disclosure_ack", False
                ),
                "staging_only": True,
            }
            if action == CODEX_CLI_RELAY_REVIEW_ACTION:
                intent_summary.update(
                    {
                        "client_kind": payload.get("client_kind", ""),
                        "client_provenance_assurance": payload.get(
                            "client_provenance_assurance", ""
                        ),
                        "client_config_profile": payload.get(
                            "client_config_profile", ""
                        ),
                        "codex_cli_path": payload.get("codex_cli_path", ""),
                        "codex_cli_sha256": payload.get("codex_cli_sha256", ""),
                        "minimum_codex_cli_version": payload.get(
                            "minimum_codex_cli_version", ""
                        ),
                        "relay_key_fingerprint": payload.get(
                            "relay_key_fingerprint", ""
                        ),
                        "reasoning_effort": payload.get("reasoning_effort", ""),
                        "reasoning_effort_namespace": payload.get(
                            "reasoning_effort_namespace", ""
                        ),
                        "identity_policy": payload.get("identity_policy", ""),
                        "credential_policy": payload.get(
                            "credential_policy", ""
                        ),
                        "billing_policy": payload.get("billing_policy", ""),
                        "fallback_policy": payload.get("fallback_policy", ""),
                        "output_token_limit_policy": payload.get(
                            "output_token_limit_policy", ""
                        ),
                        "chatgpt_subscription_used": payload.get(
                            "chatgpt_subscription_used"
                        ),
                        "openai_api_account_used": payload.get(
                            "openai_api_account_used"
                        ),
                        "token_budget": envelope.get("token_budget"),
                        "posthoc_output_token_limit": payload.get(
                            "max_output_tokens"
                        ),
                        "timeout_seconds": envelope.get("timeout_seconds"),
                    }
                )
        else:
            intent_summary = {"action": action}
        result["approval"] = {
            "approval_id": approval["approval_id"],
            "status": approval["status"],
            "reason": approval["reason"],
            "intent_sha256": approval["payload"].get("intent_sha256", ""),
            "artifact_hashes": approval["payload"].get("artifact_hashes", []),
            "route_revision": approval["payload"].get("route_revision"),
            "intent": intent_summary,
        }
        return result

    def _canonical_executor(self, *, require_enabled: bool) -> CanonicalCommitExecutor:
        try:
            executor = self.registry.get("canonical_commit")
        except ValueError as exc:
            raise GatewayError(
                "GATEWAY_CANONICAL_EXECUTOR_MISSING",
                "The canonical commit executor is not installed.",
            ) from exc
        if type(executor) is not CanonicalCommitExecutor:
            raise GatewayError(
                "GATEWAY_CANONICAL_EXECUTOR_INVALID",
                "The canonical route is not backed by the trusted commit adapter.",
            )
        if require_enabled and not self._canonical_status()["canonical_route_enabled"]:
            raise GatewayError(
                "GATEWAY_CANONICAL_ROUTE_DISABLED",
                "Canonical route is disabled or not bound to this gateway staging root.",
            )
        return executor

    def _legacy_automation_executor(self) -> LegacyCodexAutomationExecutor:
        try:
            executor = self.registry.get("legacy_codex")
        except ValueError as exc:
            raise GatewayError(
                "GATEWAY_LEGACY_EXECUTOR_MISSING",
                "Legacy Codex Automation executor is not installed.",
            ) from exc
        if type(executor) is not LegacyCodexAutomationExecutor:
            raise GatewayError(
                "GATEWAY_LEGACY_EXECUTOR_INVALID",
                "Automation route is not backed by the trusted V1 controller adapter.",
            )
        return executor

    def _codex_sdk_executor(self) -> CodexSDKExecutor:
        try:
            executor = self.registry.get("codex_sdk")
        except ValueError as exc:
            raise GatewayError(
                "GATEWAY_CODEX_SDK_EXECUTOR_MISSING",
                "Codex SDK executor is not installed.",
            ) from exc
        if type(executor) is not CodexSDKExecutor or executor.provider_config is None:
            raise GatewayError(
                "GATEWAY_CODEX_SDK_EXECUTOR_INVALID",
                "Codex SDK route is not bound to a trusted provider profile.",
            )
        return executor

    def _canonical_status(self) -> dict[str, Any]:
        try:
            candidate = self.registry.get("canonical_commit")
        except ValueError:
            candidate = None
        installed = type(candidate) is CanonicalCommitExecutor
        authorized = bool(installed and candidate.descriptor().available)
        staging_binding_ok = bool(
            installed
            and len(candidate.configured_staging_roots()) == 1
            and self._normalized_path(candidate.configured_staging_roots()[0])
            == self._normalized_path(self.staging.root)
        )
        route_enabled = bool(
            installed and authorized and staging_binding_ok and self._allow_canonical_route
        )
        return {
            "canonical_gate_installed": installed,
            "canonical_gate_authorized": authorized,
            "canonical_route_enabled": route_enabled,
            "canonical_production_writes_enabled": False,
            "canonical_staging_binding_ok": staging_binding_ok,
        }

    def _validate_staged_source(
        self,
        staged: ArtifactRef,
        *,
        allow_missing: bool = False,
    ) -> None:
        raw_source = Path(staged.uri)
        raw_root = self.staging.root
        if (
            not raw_source.is_absolute()
            or (not allow_missing and not raw_source.is_file())
            or (os.path.lexists(raw_source) and not raw_source.is_file())
            or _is_link_like(raw_source)
        ):
            raise GatewayError(
                "GATEWAY_CANONICAL_STAGING_DENIED",
                "Canonical source must be an existing regular file in gateway staging.",
            )
        if not raw_root.is_dir() or _is_link_like(raw_root):
            raise GatewayError(
                "GATEWAY_CANONICAL_STAGING_DENIED",
                "Gateway staging root is unavailable or unsafe.",
            )
        try:
            lexical_root = Path(os.path.abspath(os.fspath(raw_root)))
            lexical_source = Path(os.path.abspath(os.fspath(raw_source)))
            lexical_relative = lexical_source.relative_to(lexical_root)
            current = lexical_root
            for part in lexical_relative.parts:
                current /= part
                if _is_link_like(current):
                    raise GatewayError(
                        "GATEWAY_CANONICAL_STAGING_DENIED",
                        "Canonical source path crosses a symbolic link.",
                    )
            root = raw_root.resolve(strict=True)
            if raw_source.exists():
                source = raw_source.resolve(strict=True)
                source.relative_to(root)
        except GatewayError:
            raise
        except (OSError, ValueError) as exc:
            raise GatewayError(
                "GATEWAY_CANONICAL_STAGING_DENIED",
                "Canonical source is outside gateway staging.",
            ) from exc

    @staticmethod
    def _normalized_path(path: Path | str) -> str:
        try:
            normalized = Path(path).resolve(strict=False)
        except OSError:
            normalized = Path(os.path.abspath(os.fspath(path)))
        return os.path.normcase(os.fspath(normalized))

    @staticmethod
    def _intent_sha256(envelope: TaskEnvelope) -> str:
        return hashlib.sha256(canonical_json(envelope.to_dict()).encode("utf-8")).hexdigest()

    def _review_provider_contract(self, envelope: TaskEnvelope) -> dict[str, str]:
        executor = self.registry.get(envelope.executor)
        if getattr(executor, "client_factory", None) is not None:
            return {
                "provider_kind": "injected_test_transport",
                "provider_profile": "injected_test_transport_v1",
                "provider_assurance": "injected_test_transport",
            }
        if envelope.action == RELAY_REVIEW_ACTION:
            payload = relay_review_provider_payload()
            return {
                "provider_kind": str(payload["provider_kind"]),
                "provider_profile": str(payload["provider_profile"]),
                "provider_assurance": str(payload["provider_assurance"]),
            }
        if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
            payload = codex_cli_relay_static_provider_payload()
            return {
                "provider_kind": str(payload["provider_kind"]),
                "provider_profile": str(payload["provider_profile"]),
                "provider_assurance": str(payload["provider_assurance"]),
            }
        if envelope.action == CODEX_SDK_PROFILE_REVIEW_ACTION:
            payload = codex_sdk_profile_provider_payload(
                self._codex_sdk_executor().provider_config
            )
            return {
                "provider_kind": str(payload["provider_kind"]),
                "provider_profile": str(payload["provider_profile"]),
                "provider_assurance": str(payload["provider_assurance"]),
            }
        return {
            "provider_kind": "official_openai",
            "provider_profile": "official_openai_responses_v1",
            "provider_assurance": "official_endpoint_contract",
        }

    def _finalize_review(
        self,
        envelope: TaskEnvelope,
        result: TaskResult,
        *,
        execution_attempt_token_sha256: str,
    ) -> TaskResult:
        provider_contract = self._review_provider_contract(envelope)
        provider_data = {
            key: str(result.data.get(key) or "")
            for key in (
                "provider_kind",
                "provider_profile",
                "provider_assurance",
            )
        }
        if provider_data != provider_contract:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="REVIEW_CANARY_PROVIDER_PROVENANCE_INVALID",
                message="API review provider provenance does not match the trusted executor route.",
                metrics=result.metrics,
                data={
                    **provider_data,
                    "source_unchanged": self._source_unchanged(envelope),
                    "canonical_write": False,
                },
                external_ref=result.external_ref,
            )
        response_id = str(result.external_ref or "").strip()
        production_transport = (
            provider_contract["provider_kind"] != "injected_test_transport"
        )
        response_id_invalid = not response_id
        if production_transport and envelope.action == REVIEW_ACTION:
            response_id_invalid = response_id_invalid or not response_id.startswith("resp_")
        if production_transport and envelope.action == RELAY_REVIEW_ACTION:
            response_id_invalid = response_id_invalid or bool(
                _NON_LIVE_RESPONSE_ID.match(response_id)
            )
        if production_transport and envelope.action in {
            CODEX_CLI_RELAY_REVIEW_ACTION,
            CODEX_SDK_PROFILE_REVIEW_ACTION,
        }:
            response_id_invalid = response_id_invalid or (
                _CODEX_THREAD_ID.fullmatch(response_id) is None
            )
        if response_id_invalid:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="REVIEW_CANARY_RESPONSE_ID_INVALID",
                message="API review did not return a route-valid response identifier; no staging receipt was written.",
                metrics=result.metrics,
                data={
                    **provider_data,
                    "source_unchanged": self._source_unchanged(envelope),
                    "canonical_write": False,
                },
            )
        relay_receipt_fields: dict[str, Any] = {}
        if envelope.action in RELAY_REVIEW_ACTIONS:
            relay_receipt_fields = {
                "endpoint_base_url": str(result.data.get("endpoint_base_url") or ""),
                "endpoint_config_sha256": str(
                    result.data.get("endpoint_config_sha256") or ""
                ),
            }
            if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
                relay_receipt_fields.update(
                    {
                        key: result.data.get(key)
                        for key in _CODEX_CLI_ENVELOPE_RECEIPT_KEYS
                    }
                )
            if any(
                relay_receipt_fields.get(key) != envelope.payload.get(key)
                for key in relay_receipt_fields
            ):
                return TaskResult(
                    status=ResultStatus.FATAL_FAILURE,
                    blocker_code="REVIEW_CANARY_RELAY_ENDPOINT_RECEIPT_INVALID",
                    message="Relay result is not bound to the approved endpoint configuration.",
                    metrics=result.metrics,
                    data={
                        **provider_data,
                        **relay_receipt_fields,
                        "source_unchanged": self._source_unchanged(envelope),
                        "canonical_write": False,
                    },
                    external_ref=result.external_ref,
                )
            if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
                relay_receipt_fields.update(
                    {
                        "codex_cli_version": str(
                            result.data.get("codex_cli_version") or ""
                        ),
                        "billing_credential_fingerprint": str(
                            result.data.get("billing_credential_fingerprint") or ""
                        ),
                        "requested_model": str(
                            result.data.get("requested_model") or ""
                        ),
                        "reported_model": str(
                            result.data.get("reported_model") or ""
                        ),
                        "provider_response_id": str(
                            result.data.get("provider_response_id") or ""
                        ),
                        "provider_external_ref_kind": str(
                            result.data.get("provider_external_ref_kind") or ""
                        ),
                    }
                )
                if (
                    not relay_receipt_fields["codex_cli_version"]
                    or relay_receipt_fields["billing_credential_fingerprint"]
                    != envelope.payload.get("relay_key_fingerprint")
                    or relay_receipt_fields["requested_model"]
                    != envelope.payload.get("model")
                    or relay_receipt_fields["reported_model"]
                    or relay_receipt_fields["provider_response_id"]
                    or relay_receipt_fields["provider_external_ref_kind"]
                    != "codex_thread_id"
                ):
                    return TaskResult(
                        status=ResultStatus.FATAL_FAILURE,
                        blocker_code="REVIEW_CANARY_CODEX_CLI_IDENTITY_RECEIPT_INVALID",
                        message="Codex CLI result is not bound to its approved identity and model contract.",
                        metrics=result.metrics,
                        data={
                            **provider_data,
                            **relay_receipt_fields,
                            "source_unchanged": self._source_unchanged(envelope),
                            "canonical_write": False,
                        },
                        external_ref=result.external_ref,
                    )
        markdown = str(result.data.get("output_text") or "")
        validation = validate_review_markdown(
            markdown,
            expected_figure_count=int(envelope.payload.get("expected_figure_count") or 0),
            expected_table_count=int(envelope.payload.get("expected_table_count") or 0),
        )
        if not validation["ok"]:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="REVIEW_CANARY_OUTPUT_INVALID",
                message="API review failed the deterministic staging validator.",
                metrics=result.metrics,
                data={
                    "model": result.data.get("model", ""),
                    **provider_data,
                    **relay_receipt_fields,
                    "validation": validation,
                    "source_unchanged": self._source_unchanged(envelope),
                    "canonical_write": False,
                },
                external_ref=result.external_ref,
            )
        total_tokens = result.metrics.get("total_tokens")
        if not isinstance(total_tokens, int) or total_tokens <= 0:
            return TaskResult(
                status=ResultStatus.FATAL_FAILURE,
                blocker_code="REVIEW_CANARY_USAGE_INVALID",
                message="API review did not include positive token usage evidence.",
                metrics=result.metrics,
                data={
                    "model": result.data.get("model", ""),
                    **provider_data,
                    **relay_receipt_fields,
                    "source_unchanged": self._source_unchanged(envelope),
                    "canonical_write": False,
                },
                external_ref=result.external_ref,
            )

        # Re-read every source after the remote call. Any drift stops staging
        # completion and creates no canonical or paper-directory output.
        verify_review_inputs(envelope)
        external_ref_fields = (
            {
                "provider_external_ref_kind": "codex_thread_id",
                "provider_external_ref": result.external_ref,
                "approved_codex_cli_path": envelope.payload.get(
                    "codex_cli_path", ""
                ),
                "minimum_codex_cli_version": envelope.payload.get(
                    "minimum_codex_cli_version", ""
                ),
            }
            if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION
            else {
                "provider_external_ref_kind": "codex_thread_id",
                "provider_external_ref": result.external_ref,
            }
            if envelope.action == CODEX_SDK_PROFILE_REVIEW_ACTION
            else {"response_id": result.external_ref}
        )
        manifest = {
            "schema_version": (
                "review_canary_result_v3"
                if envelope.action
                in {
                    CODEX_CLI_RELAY_REVIEW_ACTION,
                    CODEX_SDK_PROFILE_REVIEW_ACTION,
                }
                else "review_canary_result_v2"
            ),
            "task_id": envelope.task_id,
            "run_id": envelope.run_id,
            "paper_id": envelope.paper_id,
            "action": envelope.action,
            "executor": envelope.executor,
            "model": str(result.data.get("model") or ""),
            "endpoint_origin": str(result.data.get("endpoint_origin") or ""),
            "transport_policy": str(result.data.get("transport_policy") or ""),
            **provider_data,
            **relay_receipt_fields,
            **external_ref_fields,
            "source_prompt_sha256": envelope.payload.get("source_prompt_sha256", ""),
            "rendered_prompt_sha256": envelope.payload.get("rendered_prompt_sha256", ""),
            "prompt_transform_id": envelope.payload.get("prompt_transform_id", ""),
            "outer_adapter_version": envelope.payload.get("outer_adapter_version", ""),
            "token_estimator_id": envelope.payload.get("token_estimator_id", ""),
            "evidence_scope": envelope.payload.get("evidence_scope", ""),
            "input_manifest_sha256": envelope.payload.get("input_manifest_sha256", ""),
            "execution_attempt_token_sha256": execution_attempt_token_sha256,
            "input_hashes": {
                role: value.get("sha256", "")
                for role, value in envelope.payload.get("input_roles", {}).items()
                if isinstance(value, dict)
            },
            "usage": result.metrics,
            "validation": validation,
            "source_unchanged": True,
            "canonical_write": False,
        }
        outputs, persisted_manifest = self.staging.persist_review(
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            markdown=markdown,
            manifest=manifest,
        )
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="API review canary passed validation and was written to private staging only.",
            outputs=outputs,
            metrics=result.metrics,
            data={
                "model": result.data.get("model", ""),
                "endpoint_origin": result.data.get("endpoint_origin", ""),
                "transport_policy": result.data.get("transport_policy", ""),
                **provider_data,
                **relay_receipt_fields,
                "gateway_request_id": result.data.get("gateway_request_id", ""),
                "staging_dir": str(self.staging.task_dir(envelope.run_id, envelope.task_id)),
                "validation": validation,
                "source_unchanged": True,
                "canonical_write": False,
                "output": persisted_manifest["output"],
            },
            external_ref=result.external_ref,
        )

    def _review_result_from_staging(
        self,
        envelope: TaskEnvelope,
        *,
        execution_attempt_token_sha256: str,
    ) -> TaskResult:
        outputs, manifest, markdown = self.staging.load_review(
            envelope.run_id,
            envelope.task_id,
        )
        required = {
            "schema_version",
            "task_id",
            "run_id",
            "paper_id",
            "action",
            "executor",
            "model",
            "endpoint_origin",
            "transport_policy",
            "provider_kind",
            "provider_profile",
            "provider_assurance",
            "response_id",
            "source_prompt_sha256",
            "rendered_prompt_sha256",
            "prompt_transform_id",
            "outer_adapter_version",
            "token_estimator_id",
            "evidence_scope",
            "input_manifest_sha256",
            "execution_attempt_token_sha256",
            "input_hashes",
            "usage",
            "validation",
            "source_unchanged",
            "canonical_write",
            "output",
        }
        if envelope.action in RELAY_REVIEW_ACTIONS:
            required.update({"endpoint_base_url", "endpoint_config_sha256"})
        if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
            required.remove("response_id")
            required.update(
                {
                    *_CODEX_CLI_ENVELOPE_RECEIPT_KEYS,
                    *_CODEX_CLI_RUNTIME_RECEIPT_KEYS,
                    "provider_external_ref",
                    "approved_codex_cli_path",
                    "minimum_codex_cli_version",
                }
            )
        elif envelope.action == CODEX_SDK_PROFILE_REVIEW_ACTION:
            required.remove("response_id")
            required.update(
                {
                    "provider_external_ref_kind",
                    "provider_external_ref",
                }
            )
        expected_hashes = {
            role: value.get("sha256", "")
            for role, value in envelope.payload.get("input_roles", {}).items()
            if isinstance(value, dict)
        }
        validation = validate_review_markdown(
            markdown,
            expected_figure_count=int(envelope.payload.get("expected_figure_count") or 0),
            expected_table_count=int(envelope.payload.get("expected_table_count") or 0),
        )
        expected_scalars = {
            "schema_version": (
                "review_canary_result_v3"
                if envelope.action
                in {
                    CODEX_CLI_RELAY_REVIEW_ACTION,
                    CODEX_SDK_PROFILE_REVIEW_ACTION,
                }
                else "review_canary_result_v2"
            ),
            "task_id": envelope.task_id,
            "run_id": envelope.run_id,
            "paper_id": envelope.paper_id,
            "action": envelope.action,
            "executor": envelope.executor,
            "model": envelope.payload.get("model", ""),
            "source_prompt_sha256": envelope.payload.get("source_prompt_sha256", ""),
            "rendered_prompt_sha256": envelope.payload.get("rendered_prompt_sha256", ""),
            "prompt_transform_id": envelope.payload.get("prompt_transform_id", ""),
            "outer_adapter_version": envelope.payload.get("outer_adapter_version", ""),
            "token_estimator_id": envelope.payload.get("token_estimator_id", ""),
            "evidence_scope": envelope.payload.get("evidence_scope", ""),
            "input_manifest_sha256": envelope.payload.get("input_manifest_sha256", ""),
            "execution_attempt_token_sha256": execution_attempt_token_sha256,
            **self._review_provider_contract(envelope),
        }
        if envelope.action in RELAY_REVIEW_ACTIONS:
            expected_scalars.update(
                {
                    "endpoint_base_url": envelope.payload.get(
                        "endpoint_base_url", ""
                    ),
                    "endpoint_config_sha256": envelope.payload.get(
                        "endpoint_config_sha256", ""
                    ),
                }
            )
        if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION:
            expected_scalars.update(
                {
                    **{
                        key: envelope.payload.get(key)
                        for key in _CODEX_CLI_ENVELOPE_RECEIPT_KEYS
                    },
                    "billing_credential_fingerprint": envelope.payload.get(
                        "relay_key_fingerprint", ""
                    ),
                    "requested_model": envelope.payload.get("model", ""),
                    "reported_model": "",
                    "provider_response_id": "",
                    "provider_external_ref_kind": "codex_thread_id",
                    "approved_codex_cli_path": envelope.payload.get(
                        "codex_cli_path", ""
                    ),
                    "minimum_codex_cli_version": envelope.payload.get(
                        "minimum_codex_cli_version", ""
                    ),
                }
            )
        elif envelope.action == CODEX_SDK_PROFILE_REVIEW_ACTION:
            expected_scalars["provider_external_ref_kind"] = "codex_thread_id"
        external_ref = str(
            manifest.get(
                "provider_external_ref"
                if envelope.action
                in {
                    CODEX_CLI_RELAY_REVIEW_ACTION,
                    CODEX_SDK_PROFILE_REVIEW_ACTION,
                }
                else "response_id"
            )
            or ""
        )
        if (
            set(manifest) != required
            or any(manifest.get(key) != value for key, value in expected_scalars.items())
            or manifest.get("input_hashes") != expected_hashes
            or manifest.get("validation") != validation
            or validation.get("ok") is not True
            or manifest.get("source_unchanged") is not True
            or manifest.get("canonical_write") is not False
            or not isinstance(manifest.get("usage"), dict)
            or not isinstance(manifest.get("usage", {}).get("total_tokens"), int)
            or manifest.get("usage", {}).get("total_tokens", 0) <= 0
            or not str(manifest.get("model") or "")
            or not str(manifest.get("endpoint_origin") or "")
            or not str(manifest.get("transport_policy") or "")
            or not external_ref
            or (
                envelope.action
                in {
                    CODEX_CLI_RELAY_REVIEW_ACTION,
                    CODEX_SDK_PROFILE_REVIEW_ACTION,
                }
                and _CODEX_THREAD_ID.fullmatch(external_ref) is None
            )
            or (
                envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION
                and not str(manifest.get("codex_cli_version") or "")
            )
            or (
                envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION
                and manifest.get("usage", {}).get("usage_total_source")
                not in {"input_plus_output_v1", "reported_total_tokens"}
            )
            or _SHA256.fullmatch(execution_attempt_token_sha256) is None
        ):
            raise StagingError(
                "REVIEW_CANARY_STAGING_RECEIPT_INVALID",
                "Staged review receipt does not match its immutable task envelope.",
            )
        verify_review_inputs(envelope)
        return TaskResult(
            status=ResultStatus.COMPLETED,
            message="Verified staged API review was reconciled without another model call.",
            outputs=outputs,
            metrics=dict(manifest["usage"]),
            data={
                "model": manifest["model"],
                "endpoint_origin": manifest["endpoint_origin"],
                "transport_policy": manifest["transport_policy"],
                "provider_kind": manifest["provider_kind"],
                "provider_profile": manifest["provider_profile"],
                "provider_assurance": manifest["provider_assurance"],
                **(
                    {
                        "endpoint_base_url": manifest["endpoint_base_url"],
                        "endpoint_config_sha256": manifest[
                            "endpoint_config_sha256"
                        ],
                    }
                    if envelope.action in RELAY_REVIEW_ACTIONS
                    else {}
                ),
                **(
                    {
                        key: manifest[key]
                        for key in (
                            *_CODEX_CLI_ENVELOPE_RECEIPT_KEYS,
                            *_CODEX_CLI_RUNTIME_RECEIPT_KEYS,
                        )
                    }
                    if envelope.action == CODEX_CLI_RELAY_REVIEW_ACTION
                    else {}
                ),
                "staging_dir": str(self.staging.task_dir(envelope.run_id, envelope.task_id)),
                "validation": validation,
                "source_unchanged": True,
                "canonical_write": False,
                "output": manifest["output"],
                "reconciled_from_staging": True,
            },
            external_ref=external_ref,
        )

    @staticmethod
    def _source_unchanged(envelope: TaskEnvelope) -> bool:
        try:
            verify_review_inputs(envelope)
        except ReviewCanaryError:
            return False
        return True

    def _compact_failure(
        self,
        envelope: TaskEnvelope,
        previous: TaskResult | None,
        status: ResultStatus,
        code: str,
        message: str,
        *,
        extra_data: dict[str, Any] | None = None,
    ) -> TaskResult:
        data: dict[str, Any] = {"canonical_write": False}
        if previous is not None:
            data.update(
                {
                    "model": str(previous.data.get("model") or ""),
                    "endpoint_origin": str(previous.data.get("endpoint_origin") or ""),
                    "transport_policy": str(previous.data.get("transport_policy") or ""),
                    "provider_kind": str(previous.data.get("provider_kind") or ""),
                    "provider_profile": str(previous.data.get("provider_profile") or ""),
                    "provider_assurance": str(previous.data.get("provider_assurance") or ""),
                    "endpoint_base_url": str(previous.data.get("endpoint_base_url") or ""),
                    "endpoint_config_sha256": str(
                        previous.data.get("endpoint_config_sha256") or ""
                    ),
                }
            )
            if envelope.action in REVIEW_ACTIONS:
                data["source_unchanged"] = self._source_unchanged(envelope)
        if extra_data:
            data.update(extra_data)
        return TaskResult(
            status=status,
            blocker_code=code,
            message=message,
            metrics=dict(previous.metrics) if previous is not None else {},
            data=data,
            external_ref=previous.external_ref if previous is not None else "",
        )


def default_gateway() -> ExecutionGateway:
    return ExecutionGateway(
        database_path=DATABASE_PATH,
        staging_root=STATE_DIR / "staging",
    )
