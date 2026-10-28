"""Current-user encrypted control-plane bearer session."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..credential_vault import protect_text, unprotect_text
from ..io_utils import now_iso, read_json, write_json_atomic
from .contracts import ContractError
from .paths import session_record_path


SESSION_SCHEMA = "uap.control-session/v1"


class CommercialSessionStore:
    def __init__(
        self,
        path: Path | str | None = None,
        *,
        protector: Callable[[str], str] = protect_text,
        unprotector: Callable[[str], str] = unprotect_text,
    ) -> None:
        self.path = Path(path) if path is not None else session_record_path()
        self._protector = protector
        self._unprotector = unprotector

    def save(self, *, user_id: str, email_hint: str, bearer_token: str) -> None:
        if not user_id.strip() or not bearer_token:
            raise ContractError("Commercial session is incomplete.")
        # Store only a non-address display hint; the full e-mail is not needed
        # for licence refresh and should not be copied across local state files.
        safe_hint = email_hint.split("@", 1)[0][:3] + "***" if email_hint else ""
        write_json_atomic(
            self.path,
            {
                "schema": SESSION_SCHEMA,
                "user_id": user_id.strip(),
                "email_hint": safe_hint,
                "bearer_token_ciphertext": self._protector(bearer_token),
                "updated_at": now_iso(),
            },
        )

    def load(self) -> tuple[str, str, str] | None:
        if not self.path.exists():
            return None
        data = read_json(self.path)
        expected = {
            "schema",
            "user_id",
            "email_hint",
            "bearer_token_ciphertext",
            "updated_at",
        }
        if not isinstance(data, dict) or data.get("schema") != SESSION_SCHEMA or set(data) != expected:
            raise ContractError("Commercial session cache is malformed.")
        token = self._unprotector(str(data.get("bearer_token_ciphertext") or ""))
        if not token:
            raise ContractError("Commercial session token is unavailable.")
        return str(data["user_id"]), str(data["email_hint"]), token

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
