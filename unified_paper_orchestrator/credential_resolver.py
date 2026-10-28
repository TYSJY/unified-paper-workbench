from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .credential_vault import (
    ACCOUNT_VAULT_REF_PREFIX,
    CREDENTIAL_REF_PREFIX,
    DpapiCredentialVault,
)


class CredentialVault(Protocol):
    def reveal(self, credential_ref: str) -> str: ...


class CredentialResolutionError(RuntimeError):
    """Value-free failure raised when an account secret cannot be resolved."""


class SmartCredentialResolver:
    """Resolve one Smart account without persisting or formatting its secret.

    A present ``credential_ref`` is authoritative.  If that reference is
    missing, unsupported, corrupt, or empty, resolution fails closed and never
    falls back to a retained legacy plaintext value.  Plaintext is used only
    for records that do not yet have a reference.
    """

    def __init__(
        self,
        vault_path: Path | str,
        *,
        vault: CredentialVault | None = None,
        account_vault_path: Path | str | None = None,
        account_vault: CredentialVault | None = None,
    ) -> None:
        self.vault_path = Path(vault_path)
        self.vault = vault or DpapiCredentialVault(self.vault_path)
        self.account_vault_path = (
            Path(account_vault_path) if account_vault_path is not None else None
        )
        self.account_vault = (
            account_vault
            if account_vault is not None
            else (
                DpapiCredentialVault(self.account_vault_path)
                if self.account_vault_path is not None
                else None
            )
        )

    @staticmethod
    def source(account: dict[str, Any]) -> str:
        credential_ref = str(account.get("credential_ref") or "").strip()
        if credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
            return "dpapi_account_vault"
        if credential_ref:
            return "dpapi"
        if str(account.get("password") or "").strip():
            return "legacy_plaintext"
        return "none"

    def resolve_password(self, account: dict[str, Any]) -> str:
        if not isinstance(account, dict):
            raise CredentialResolutionError("账号凭据记录格式不合法。")
        credential_ref = str(account.get("credential_ref") or "").strip()
        if credential_ref:
            backend = self.vault
            routed_ref = credential_ref
            if credential_ref.startswith(ACCOUNT_VAULT_REF_PREFIX):
                if self.account_vault is None:
                    raise CredentialResolutionError("账号凭据保险库尚未配置。")
                record_id = credential_ref[len(ACCOUNT_VAULT_REF_PREFIX) :]
                if not record_id:
                    raise CredentialResolutionError("账号凭据引用为空。")
                backend = self.account_vault
                routed_ref = f"{CREDENTIAL_REF_PREFIX}{record_id}"
            elif not credential_ref.startswith(CREDENTIAL_REF_PREFIX):
                raise CredentialResolutionError("账号凭据引用类型不受支持。")
            try:
                password = backend.reveal(routed_ref)
            except Exception:
                # The vault/backend is an untrusted secret boundary.  Its
                # exception text can contain decrypted values, record bodies,
                # paths, or provider diagnostics, so no backend exception may
                # cross this API even when it is an unexpected implementation
                # error.  BaseException (interrupt/exit) is intentionally not
                # swallowed.
                raise CredentialResolutionError(
                    "账号凭据引用无法由当前 Windows 用户安全读取。"
                ) from None
            if not password:
                raise CredentialResolutionError("账号凭据引用没有可用密码。")
            return password

        password = str(account.get("password") or "")
        if not password:
            raise CredentialResolutionError("账号尚未配置可用凭据。")
        return password

    def is_available(self, account: dict[str, Any]) -> bool:
        try:
            password = self.resolve_password(account)
        except CredentialResolutionError:
            return False
        available = bool(password)
        password = ""
        return available
