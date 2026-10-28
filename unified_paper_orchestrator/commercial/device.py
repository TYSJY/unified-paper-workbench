"""Privacy-preserving installation identity.

No hardware serial number, MAC address, Windows SID, file path, or user name is
collected.  A random UUID is generated once and can be reset by removing the
commercial state directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..io_utils import now_iso, read_json, write_json_atomic
from .contracts import ContractError
from .paths import device_record_path


DEVICE_SCHEMA = "uap.device/v1"


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    installation_id: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": DEVICE_SCHEMA,
            "device_id": self.device_id,
            "installation_id": self.installation_id,
            "created_at": self.created_at,
        }


class DeviceIdentityStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else device_record_path()

    def get_or_create(self) -> DeviceIdentity:
        if self.path.exists():
            return self.load()
        identity = DeviceIdentity(
            device_id=f"dev-{uuid4()}",
            installation_id=f"inst-{uuid4()}",
            created_at=now_iso(),
        )
        write_json_atomic(self.path, identity.to_dict())
        return identity

    def load(self) -> DeviceIdentity:
        data = read_json(self.path)
        if not isinstance(data, dict) or data.get("schema") != DEVICE_SCHEMA:
            raise ContractError("Local device identity is malformed.")
        expected = {"schema", "device_id", "installation_id", "created_at"}
        if set(data) != expected:
            raise ContractError("Local device identity fields are invalid.")
        values = [data.get("device_id"), data.get("installation_id"), data.get("created_at")]
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ContractError("Local device identity contains an empty value.")
        return DeviceIdentity(
            device_id=data["device_id"].strip(),
            installation_id=data["installation_id"].strip(),
            created_at=data["created_at"].strip(),
        )
