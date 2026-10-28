from __future__ import annotations

import hashlib

from ..contracts import ResultStatus, TaskEnvelope, TaskResult, canonical_json
from .openai_review import OpenAIReviewExecutor


QILIN_RELAY_BASE_URL = "https://www.qilinapi.com"
QILIN_RELAY_PROVIDER_PROFILE = "qilinapi_responses_v1"
QILIN_RELAY_PROVIDER_KIND = "third_party_openai_compatible_relay"
QILIN_RELAY_PROVIDER_ASSURANCE = "third_party_claim_unverified"
QILIN_RELAY_WIRE_API = "responses"
RELAY_API_KEY_ENV = "UNIFIED_RELAY_API_KEY"
RELAY_MODEL_ENV = "UNIFIED_RELAY_MODEL"
RELAY_LOOPBACK_PROXY_ENV = "UNIFIED_RELAY_HTTPS_PROXY"


def relay_endpoint_config() -> dict[str, str]:
    """Return the public, secret-free provider binding stored in task intent."""

    return {
        "provider_kind": QILIN_RELAY_PROVIDER_KIND,
        "provider_profile": QILIN_RELAY_PROVIDER_PROFILE,
        "provider_assurance": QILIN_RELAY_PROVIDER_ASSURANCE,
        "wire_api": QILIN_RELAY_WIRE_API,
        "endpoint_base_url": QILIN_RELAY_BASE_URL,
    }


def relay_endpoint_config_sha256() -> str:
    return hashlib.sha256(canonical_json(relay_endpoint_config()).encode("utf-8")).hexdigest()


class OpenAICompatibleRelayReviewExecutor(OpenAIReviewExecutor):
    """Pinned third-party Responses-compatible review adapter.

    This executor is intentionally distinct from ``openai_review``.  Its
    endpoint, credential namespace, task action, approval, and audit identity
    cannot be confused with official OpenAI transport.
    """

    name = "openai_compatible_relay_review"
    descriptor_kind = "third_party_responses_compatible_staging_review"
    api_key_env = RELAY_API_KEY_ENV
    model_env = RELAY_MODEL_ENV
    proxy_env = RELAY_LOOPBACK_PROXY_ENV
    base_url = QILIN_RELAY_BASE_URL
    provider_kind = QILIN_RELAY_PROVIDER_KIND
    provider_profile = QILIN_RELAY_PROVIDER_PROFILE
    provider_assurance = QILIN_RELAY_PROVIDER_ASSURANCE

    def _execute(self, task: TaskEnvelope) -> TaskResult:
        expected = {
            **relay_endpoint_config(),
            "endpoint_config_sha256": relay_endpoint_config_sha256(),
            "third_party_data_disclosure_ack": True,
        }
        if any(task.payload.get(key) != value for key, value in expected.items()):
            return TaskResult(
                status=ResultStatus.BLOCKED,
                blocker_code="RELAY_ENDPOINT_BINDING_MISMATCH",
                message="Relay provider intent no longer matches the pinned endpoint contract; no request was sent.",
                data={
                    "endpoint_origin": self.base_url,
                    "transport_policy": "endpoint_binding_rejected_before_request",
                    "endpoint_base_url": self.base_url,
                    "endpoint_config_sha256": relay_endpoint_config_sha256(),
                    "wire_api": QILIN_RELAY_WIRE_API,
                },
            )

        result = super()._execute(task)
        if result.blocker_code.startswith("OPENAI_"):
            result.blocker_code = f"RELAY_{result.blocker_code.removeprefix('OPENAI_')}"
        if "OPENAI_MODEL" in result.message:
            result.message = result.message.replace("OPENAI_MODEL", RELAY_MODEL_ENV)
        result.data = {
            **result.data,
            "endpoint_base_url": self.base_url,
            "endpoint_config_sha256": relay_endpoint_config_sha256(),
            "wire_api": QILIN_RELAY_WIRE_API,
        }
        return result
