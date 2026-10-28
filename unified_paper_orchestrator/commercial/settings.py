"""Local commercial settings with a disabled-by-default compatibility mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..io_utils import read_json, write_json_atomic
from .client import ControlPlaneClient
from .contracts import ContractError
from .paths import commercial_state_root


SETTINGS_SCHEMA = "uap.commercial-settings/v1"


@dataclass(frozen=True)
class CommercialSettings:
    enabled: bool = False
    control_plane_url: str = "https://licensing.invalid"
    release_channel: str = "stable"
    diagnostics_opt_in: bool = False
    development_mode: bool = False
    development_public_key_path: str = ""

    def __post_init__(self) -> None:
        if self.release_channel not in {"stable", "preview"}:
            raise ContractError("Release channel must be stable or preview.")
        if not all(
            isinstance(value, bool)
            for value in (
                self.enabled,
                self.diagnostics_opt_in,
                self.development_mode,
            )
        ):
            raise ContractError("Commercial settings flags must be boolean.")
        if self.enabled:
            ControlPlaneClient(self.control_plane_url)
        if self.development_public_key_path and not self.development_mode:
            raise ContractError("A development signing key requires development mode.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SETTINGS_SCHEMA,
            "enabled": self.enabled,
            "control_plane_url": self.control_plane_url,
            "release_channel": self.release_channel,
            "diagnostics_opt_in": self.diagnostics_opt_in,
            "development_mode": self.development_mode,
            "development_public_key_path": self.development_public_key_path,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CommercialSettings":
        if not isinstance(value, dict):
            raise ContractError("Commercial settings must be an object.")
        expected = {
            "schema",
            "enabled",
            "control_plane_url",
            "release_channel",
            "diagnostics_opt_in",
            "development_mode",
            "development_public_key_path",
        }
        if value.get("schema") != SETTINGS_SCHEMA or set(value) != expected:
            raise ContractError("Commercial settings schema or fields are invalid.")
        return cls(
            enabled=value["enabled"],
            control_plane_url=str(value["control_plane_url"] or "").strip(),
            release_channel=str(value["release_channel"] or "").strip(),
            diagnostics_opt_in=value["diagnostics_opt_in"],
            development_mode=value["development_mode"],
            development_public_key_path=str(
                value["development_public_key_path"] or ""
            ).strip(),
        )


class CommercialSettingsStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else commercial_state_root() / "settings.json"
        )

    def load(self) -> CommercialSettings:
        if not self.path.exists():
            return CommercialSettings()
        return CommercialSettings.from_dict(read_json(self.path))

    def save(self, settings: CommercialSettings) -> CommercialSettings:
        write_json_atomic(self.path, settings.to_dict())
        return settings
