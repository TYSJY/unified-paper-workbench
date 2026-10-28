"""Optional commercial-control capabilities for the desktop application.

The existing paper workflows do not import this package at startup.  Commercial
features are activated explicitly, which keeps the 0.11.6 workflow behaviour
unchanged while the 0.12 control plane is introduced.
"""

from .contracts import (
    ContractError,
    Entitlements,
    LicenseClaims,
    ReleaseArtifact,
    ReleaseManifest,
    SignedEnvelope,
)

__all__ = [
    "ContractError",
    "Entitlements",
    "LicenseClaims",
    "ReleaseArtifact",
    "ReleaseManifest",
    "SignedEnvelope",
]
