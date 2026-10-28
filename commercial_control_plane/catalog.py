"""Server-side subscription catalogue.

The desktop trusts only entitlements copied into a signed licence.  This module
is therefore the single policy source for the development control plane.
"""

from __future__ import annotations

from unified_paper_orchestrator.commercial.contracts import Entitlements


PRODUCT_ID = "unified-paper-workbench"
DESKTOP_AUDIENCE = "unified-paper-desktop"
ALL_WORKFLOW_FEATURES = (
    "writing",
    "figures",
    "journal_author",
    "smart_submission",
    "signed_updates",
)


PLAN_CATALOG: dict[str, Entitlements] = {
    "trial": Entitlements(
        max_devices=1,
        max_concurrent_papers=2,
        features=ALL_WORKFLOW_FEATURES,
        update_channels=("stable", "preview"),
        diagnostics_upload=False,
    ),
    "professional": Entitlements(
        max_devices=2,
        max_concurrent_papers=8,
        features=ALL_WORKFLOW_FEATURES + ("priority_adapters",),
        update_channels=("stable", "preview"),
        diagnostics_upload=True,
    ),
    "lab": Entitlements(
        max_devices=10,
        max_concurrent_papers=32,
        features=ALL_WORKFLOW_FEATURES + ("priority_adapters", "lab_collaboration"),
        update_channels=("stable", "preview"),
        diagnostics_upload=True,
    ),
}


def public_catalog() -> dict:
    return {
        "schema": "uap.plan-catalog/v1",
        "product_id": PRODUCT_ID,
        "plans": {
            name: entitlements.to_dict()
            for name, entitlements in sorted(PLAN_CATALOG.items())
        },
    }
