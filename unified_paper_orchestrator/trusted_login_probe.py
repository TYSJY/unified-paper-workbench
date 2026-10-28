"""Audited orchestration for one local Smart Submission login-session probe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .account_site_store import AccountSiteStore, RegistryValidationError
from .browser_session import (
    ALLOWED_CDP_PORTS,
    BrowserSessionEvidenceKeyStore,
    BrowserSessionEvidenceSigner,
    BrowserSessionProbe,
    BrowserSessionProbeError,
    CdpProbeTransport,
    LocalCdpProbeTransport,
    normalize_origin,
)
from .config import (
    ACCOUNT_VAULT_PATH,
    BROWSER_SESSION_EVIDENCE_KEY_PATH,
    CREDENTIAL_VAULT_PATH,
    LEGACY_CREDENTIAL_VAULT_PATH,
    STATE_DIR,
)


SMART_LOGIN_PROBE_OK = "SMART_SESSION_PROBE_RECORDED"


def probe_and_record_browser_session(
    *,
    account_id: str,
    system_id: str,
    origin: str,
    ports: tuple[int, ...] = ALLOWED_CDP_PORTS,
    credential_path: Path | str | None = None,
    site_registry_path: Path | str | None = None,
    key_path: Path | str | None = None,
    store: AccountSiteStore | None = None,
    transport: CdpProbeTransport | None = None,
    signer: BrowserSessionEvidenceSigner | None = None,
) -> dict[str, Any]:
    """Probe, sign, and transactionally record one value-free session receipt.

    Backend failures are intentionally collapsed to stable messages. No CDP
    exception text or raw target/page value can reach CLI output or audit data.
    """

    try:
        clean_account_id = str(account_id or "").strip()
        clean_system_id = str(system_id or "").strip()
        clean_origin = normalize_origin(origin)
        smart_dir = STATE_DIR / "smart_submission"
        active_store = store or AccountSiteStore(
            credential_path or smart_dir / "credential_store.local.json",
            site_registry_path or smart_dir / "site_registry.json",
            vault_path=ACCOUNT_VAULT_PATH,
            legacy_vault_path=LEGACY_CREDENTIAL_VAULT_PATH,
            runtime_vault_path=CREDENTIAL_VAULT_PATH,
            smart_state_path=smart_dir / "smart_submission_state.json",
            unified_state_path=STATE_DIR / "unified_state.json",
        )
        active_signer = signer or BrowserSessionEvidenceSigner(
            BrowserSessionEvidenceKeyStore(key_path or BROWSER_SESSION_EVIDENCE_KEY_PATH)
        )
        verified_origins = active_store.verified_browser_origins(clean_account_id, clean_system_id)
        evidence = BrowserSessionProbe(
            transport=transport or LocalCdpProbeTransport(),
            signer=active_signer,
            ports=ports,
        ).probe(
            account_id=clean_account_id,
            target_origin=clean_origin,
            verified_origins=verified_origins,
        )
        receipt = active_store.record_browser_session_evidence(
            clean_account_id,
            clean_system_id,
            clean_origin,
            evidence,
            signer=active_signer,
        )
        return {
            "ok": True,
            "code": SMART_LOGIN_PROBE_OK,
            **receipt,
            "page_state": {
                "document_ready": True,
                "exact_origin": True,
                "login_challenge_visible": False,
                "authenticated_ui_visible": True,
            },
            "value_free": True,
        }
    except BrowserSessionProbeError as exc:
        return {**exc.as_dict(), "value_free": True}
    except (RegistryValidationError, ValueError):
        return {
            "ok": False,
            "code": "SMART_SESSION_PROBE_BINDING_INVALID",
            "message": "the account, verified system, and target origin binding is invalid",
            "value_free": True,
        }
    except Exception:
        return {
            "ok": False,
            "code": "SMART_SESSION_PROBE_FAILED",
            "message": "the local value-free browser-session probe failed safely",
            "value_free": True,
        }
