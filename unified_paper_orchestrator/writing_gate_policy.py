from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Any

from .io_utils import FileLock, now_iso, read_json, write_json_atomic


WRITING_GATE_POLICY_SCHEMA = "unified-paper-orchestrator/writing-gate-policy/v1"
STEP5_DATA_CONFIRMATION_GATE = "step5_data_confirmation"
STRICT_CONFIRMATION_MODE = "strict_confirmation"
AUTO_TRUST_MODE = "auto_trust"
WRITING_GATE_MODES = frozenset({STRICT_CONFIRMATION_MODE, AUTO_TRUST_MODE})
_PASSWORD_ALGORITHM = "pbkdf2-sha256"
_DEFAULT_PASSWORD_ITERATIONS = 260_000
_HISTORY_LIMIT = 100


class WritingGatePolicyError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WritingGatePolicyStore:
    """Workspace-scoped policy for human writing gates.

    The password is only a local guard against accidental UI changes.  It is
    never persisted in plaintext.  A missing policy file always means the
    strict/manual default; a malformed existing file fails closed.
    """

    def __init__(self, path: Path | str, *, password_iterations: int = _DEFAULT_PASSWORD_ITERATIONS) -> None:
        self.path = Path(path)
        if isinstance(password_iterations, bool) or int(password_iterations) < 1_000:
            raise ValueError("password_iterations must be at least 1000")
        self.password_iterations = int(password_iterations)
        self.lock_path = self.path.with_suffix(".lock")

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "schema": WRITING_GATE_POLICY_SCHEMA,
            "revision": 0,
            "gates": {
                STEP5_DATA_CONFIRMATION_GATE: {
                    "mode": STRICT_CONFIRMATION_MODE,
                    "updated_at": "",
                }
            },
            "admin_password": None,
            "history": [],
        }

    @staticmethod
    def _validate_state(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise WritingGatePolicyError(
                "WRITING_GATE_POLICY_INVALID",
                "写作数据关口策略文件根节点必须是对象；已按严格确认模式停止自动放行。",
            )
        if state.get("schema") != WRITING_GATE_POLICY_SCHEMA:
            raise WritingGatePolicyError(
                "WRITING_GATE_POLICY_SCHEMA_INVALID",
                "写作数据关口策略文件版本不受支持；已按严格确认模式停止自动放行。",
            )
        revision = state.get("revision")
        gates = state.get("gates")
        gate = gates.get(STEP5_DATA_CONFIRMATION_GATE) if isinstance(gates, dict) else None
        mode = gate.get("mode") if isinstance(gate, dict) else None
        history = state.get("history")
        password = state.get("admin_password")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口策略修订号无效。")
        if mode not in WRITING_GATE_MODES:
            raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作 Step 5 数据确认策略无效。")
        if not isinstance(history, list):
            raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口策略审计记录无效。")
        if password is not None:
            if not isinstance(password, dict):
                raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口管理密码记录无效。")
            required = {"algorithm", "iterations", "salt_b64", "digest_b64"}
            if not required.issubset(password):
                raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口管理密码记录不完整。")
            if password.get("algorithm") != _PASSWORD_ALGORITHM:
                raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口密码算法不受支持。")
            iterations = password.get("iterations")
            if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1_000:
                raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口密码参数无效。")
            try:
                salt = base64.b64decode(str(password.get("salt_b64") or ""), validate=True)
                digest = base64.b64decode(str(password.get("digest_b64") or ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise WritingGatePolicyError(
                    "WRITING_GATE_POLICY_INVALID",
                    "写作数据关口密码记录无法解码。",
                ) from exc
            if len(salt) < 16 or len(digest) != 32:
                raise WritingGatePolicyError("WRITING_GATE_POLICY_INVALID", "写作数据关口密码记录长度无效。")
        return state

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            state = read_json(self.path, {})
        except Exception as exc:
            raise WritingGatePolicyError(
                "WRITING_GATE_POLICY_UNREADABLE",
                "写作数据关口策略文件无法读取；已按严格确认模式停止自动放行。",
            ) from exc
        return self._validate_state(state)

    @staticmethod
    def _mode_label(mode: str) -> str:
        return "自动信任并继续" if mode == AUTO_TRUST_MODE else "严格确认（默认）"

    def load_public(self) -> dict[str, Any]:
        state = self._load()
        gate = state["gates"][STEP5_DATA_CONFIRMATION_GATE]
        mode = str(gate["mode"])
        return {
            "schema": WRITING_GATE_POLICY_SCHEMA,
            "gate_id": STEP5_DATA_CONFIRMATION_GATE,
            "mode": mode,
            "mode_label": self._mode_label(mode),
            "auto_trust_enabled": mode == AUTO_TRUST_MODE,
            "password_configured": isinstance(state.get("admin_password"), dict),
            "revision": int(state["revision"]),
            "updated_at": str(gate.get("updated_at") or ""),
            "path": str(self.path),
        }

    def auto_trust_snapshot(self) -> dict[str, Any] | None:
        public = self.load_public()
        if not public["auto_trust_enabled"]:
            return None
        return {
            "schema": public["schema"],
            "gate_id": public["gate_id"],
            "mode": public["mode"],
            "revision": public["revision"],
            "updated_at": public["updated_at"],
        }

    @staticmethod
    def _normalize_password(password: str) -> str:
        value = str(password or "")
        if not 4 <= len(value) <= 128:
            raise WritingGatePolicyError(
                "WRITING_GATE_PASSWORD_INVALID",
                "管理密码长度必须为 4-128 个字符。",
            )
        if any(ord(char) < 32 for char in value):
            raise WritingGatePolicyError("WRITING_GATE_PASSWORD_INVALID", "管理密码不能包含控制字符。")
        return value

    def _new_password_record(self, password: str) -> dict[str, Any]:
        normalized = self._normalize_password(password)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            normalized.encode("utf-8"),
            salt,
            self.password_iterations,
        )
        return {
            "algorithm": _PASSWORD_ALGORITHM,
            "iterations": self.password_iterations,
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "digest_b64": base64.b64encode(digest).decode("ascii"),
        }

    def _verify_password(self, record: dict[str, Any], password: str) -> bool:
        normalized = self._normalize_password(password)
        salt = base64.b64decode(str(record["salt_b64"]), validate=True)
        expected = base64.b64decode(str(record["digest_b64"]), validate=True)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            normalized.encode("utf-8"),
            salt,
            int(record["iterations"]),
        )
        return hmac.compare_digest(actual, expected)

    def set_step5_mode(
        self,
        mode: str,
        *,
        password: str,
        password_confirmation: str = "",
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in WRITING_GATE_MODES:
            raise WritingGatePolicyError("WRITING_GATE_MODE_INVALID", "Step 5 数据确认策略无效。")

        with FileLock(self.lock_path):
            state = self._load()
            password_record = state.get("admin_password")
            if isinstance(password_record, dict):
                if not self._verify_password(password_record, password):
                    raise WritingGatePolicyError("WRITING_GATE_PASSWORD_INCORRECT", "管理密码错误，策略未更改。")
            else:
                normalized_password = self._normalize_password(password)
                if normalized_password != str(password_confirmation or ""):
                    raise WritingGatePolicyError(
                        "WRITING_GATE_PASSWORD_CONFIRMATION_MISMATCH",
                        "两次输入的管理密码不一致，策略未更改。",
                    )
                state["admin_password"] = self._new_password_record(normalized_password)

            gate = state["gates"][STEP5_DATA_CONFIRMATION_GATE]
            previous_mode = str(gate["mode"])
            timestamp = now_iso()
            revision = int(state["revision"]) + 1
            gate.update({"mode": normalized_mode, "updated_at": timestamp})
            state["revision"] = revision
            history = state.setdefault("history", [])
            history.append(
                {
                    "at": timestamp,
                    "event": "step5_data_confirmation_mode_changed",
                    "gate_id": STEP5_DATA_CONFIRMATION_GATE,
                    "from": previous_mode,
                    "to": normalized_mode,
                    "revision": revision,
                }
            )
            state["history"] = history[-_HISTORY_LIMIT:]
            write_json_atomic(self.path, state)

        public = self.load_public()
        public.update({"previous_mode": previous_mode, "changed": previous_mode != normalized_mode})
        return public
