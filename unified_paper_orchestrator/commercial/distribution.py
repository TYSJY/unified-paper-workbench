"""Immutable distribution policy embedded into each desktop build.

The source tree defaults to the internal compatibility edition.  A customer
build replaces only the packaged JSON policy at build time; the customer UI
cannot change the control-plane origin or turn commercial enforcement off.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..io_utils import read_json
from .contracts import ContractError


DISTRIBUTION_POLICY_SCHEMA = "uap.distribution-policy/v1"
VALID_EDITIONS = {"internal", "customer"}


def _https_or_loopback(value: str, field: str) -> str:
    clean = str(value or "").strip().rstrip("/")
    parsed = urlparse(clean)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ContractError(f"{field} must use HTTPS except for loopback testing.")
    if not parsed.netloc:
        raise ContractError(f"{field} is invalid.")
    return clean


def _https_url(value: str, field: str) -> str:
    clean = str(value or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ContractError(f"{field} must be an HTTPS URL.")
    return clean.rstrip("/")


@dataclass(frozen=True)
class DistributionPolicy:
    edition: str
    commercial_enforcement: bool
    allow_runtime_configuration: bool
    allow_development_keys: bool
    control_plane_url: str
    account_portal_url: str
    pricing_url: str
    support_url: str
    privacy_url: str
    terms_url: str
    release_channel: str
    trial_days: int
    product_name: str = "Unified Paper Workbench"

    def __post_init__(self) -> None:
        if self.edition not in VALID_EDITIONS:
            raise ContractError("Distribution edition must be internal or customer.")
        flags = (
            self.commercial_enforcement,
            self.allow_runtime_configuration,
            self.allow_development_keys,
        )
        if not all(isinstance(item, bool) for item in flags):
            raise ContractError("Distribution policy flags must be boolean.")
        if self.release_channel not in {"stable", "preview"}:
            raise ContractError("Distribution release channel must be stable or preview.")
        if isinstance(self.trial_days, bool) or not isinstance(self.trial_days, int):
            raise ContractError("Distribution trial_days must be an integer.")
        if not 1 <= self.trial_days <= 90:
            raise ContractError("Distribution trial_days must be between 1 and 90.")
        if not str(self.product_name or "").strip():
            raise ContractError("Distribution product_name must not be empty.")
        _https_or_loopback(self.control_plane_url, "control_plane_url")
        for field in (
            "account_portal_url",
            "pricing_url",
            "support_url",
            "privacy_url",
            "terms_url",
        ):
            _https_url(getattr(self, field), field)
        if self.edition == "customer":
            if not self.commercial_enforcement:
                raise ContractError("Customer distributions must enforce commercial access.")
            if self.allow_runtime_configuration:
                raise ContractError(
                    "Customer distributions cannot expose runtime commercial configuration."
                )
            if self.allow_development_keys:
                raise ContractError(
                    "Customer distributions cannot trust development signing keys."
                )

    @property
    def customer_edition(self) -> bool:
        return self.edition == "customer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DISTRIBUTION_POLICY_SCHEMA,
            "edition": self.edition,
            "commercial_enforcement": self.commercial_enforcement,
            "allow_runtime_configuration": self.allow_runtime_configuration,
            "allow_development_keys": self.allow_development_keys,
            "control_plane_url": self.control_plane_url,
            "account_portal_url": self.account_portal_url,
            "pricing_url": self.pricing_url,
            "support_url": self.support_url,
            "privacy_url": self.privacy_url,
            "terms_url": self.terms_url,
            "release_channel": self.release_channel,
            "trial_days": self.trial_days,
            "product_name": self.product_name,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DistributionPolicy":
        if not isinstance(value, dict):
            raise ContractError("Distribution policy must be an object.")
        expected = {
            "schema",
            "edition",
            "commercial_enforcement",
            "allow_runtime_configuration",
            "allow_development_keys",
            "control_plane_url",
            "account_portal_url",
            "pricing_url",
            "support_url",
            "privacy_url",
            "terms_url",
            "release_channel",
            "trial_days",
            "product_name",
        }
        if value.get("schema") != DISTRIBUTION_POLICY_SCHEMA or set(value) != expected:
            raise ContractError("Distribution policy schema or fields are invalid.")
        return cls(
            edition=str(value["edition"] or "").strip().casefold(),
            commercial_enforcement=value["commercial_enforcement"],
            allow_runtime_configuration=value["allow_runtime_configuration"],
            allow_development_keys=value["allow_development_keys"],
            control_plane_url=str(value["control_plane_url"] or "").strip(),
            account_portal_url=str(value["account_portal_url"] or "").strip(),
            pricing_url=str(value["pricing_url"] or "").strip(),
            support_url=str(value["support_url"] or "").strip(),
            privacy_url=str(value["privacy_url"] or "").strip(),
            terms_url=str(value["terms_url"] or "").strip(),
            release_channel=str(value["release_channel"] or "").strip().casefold(),
            trial_days=value["trial_days"],
            product_name=str(value["product_name"] or "").strip(),
        )


def default_distribution_policy_path() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return (
            root
            / "unified_paper_orchestrator"
            / "commercial"
            / "distribution_policy.json"
        )
    return Path(__file__).with_name("distribution_policy.json")


def load_distribution_policy(
    path: Path | str | None = None,
) -> DistributionPolicy:
    policy_path = Path(path) if path is not None else default_distribution_policy_path()
    if not policy_path.is_file():
        raise ContractError("Embedded distribution policy is missing.")
    return DistributionPolicy.from_dict(read_json(policy_path))
