"""Complete model/list reads and secret-free cache provenance."""
from __future__ import annotations

import json
import inspect
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from .codex_runtime import runtime_identity
from .step_execution import normalize_model_catalog

CATALOG_MAX_AGE_SECONDS = 3600


async def read_sdk_models(codex: Any) -> list[dict[str, Any]]:
    page = codex.models(include_hidden=False)
    if inspect.isawaitable(page):
        page = await page
    models = normalize_model_catalog(page)
    seen: set[str] = set()
    while True:
        cursor = (page.get("nextCursor", page.get("next_cursor")) if isinstance(page, dict)
                  else getattr(page, "next_cursor", None))
        if not cursor:
            break
        if cursor in seen or len(seen) >= 100:
            raise RuntimeError("SDK model pagination did not finish")
        seen.add(cursor)
        # SDK 0.147's convenience method omits cursor. Keep its typed transport
        # in one adapter, and fail rather than silently truncate future catalogs.
        request = getattr(getattr(codex, "_client", None), "request", None)
        if not callable(request):
            raise RuntimeError("SDK model pagination is unsupported")
        from openai_codex.generated.v2_all import ModelListResponse
        page = await request("model/list", {"includeHidden": False, "cursor": cursor},
                             response_model=ModelListResponse)
        models.extend(normalize_model_catalog(page))
    unique = {item["model"]: item for item in models if not item["hidden"]}
    return sorted(unique.values(), key=lambda item: (not item["is_default"], item["display_name"].casefold()))


def catalog_provenance(codex_home: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    identity = runtime_identity()
    try:
        sdk_version = metadata.version("openai-codex")
        runtime_version = (identity["version"] if identity else metadata.version("openai-codex-cli-bin"))
    except metadata.PackageNotFoundError:
        sdk_version, runtime_version = "", identity["version"] if identity else ""
    result: dict[str, Any] = {
        "sdk_version": sdk_version, "runtime_version": runtime_version,
        "checked_at": now.isoformat(), "cache_fetched_at": "", "cache_state": "unknown",
        "source": "codex_app_server_model_list",
    }
    # Only project safe metadata, never auth/config/account identity fields.
    try:
        cache = json.loads((Path(codex_home) / "models_cache.json").read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(str(cache["fetched_at"]).replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            return result
        age = (now - fetched).total_seconds()
        same_runtime = str(cache.get("client_version", "")) == runtime_version
        result["cache_fetched_at"] = fetched.isoformat()
        result["cache_state"] = "fresh" if same_runtime and -60 <= age <= CATALOG_MAX_AGE_SECONDS else "stale"
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result
