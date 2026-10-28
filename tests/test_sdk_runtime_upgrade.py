from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from unified_paper_orchestrator import codex_runtime, sdk_model_catalog
from unified_paper_orchestrator.step_execution import normalize_execution_profile, normalize_model_catalog
from unified_paper_orchestrator.ui_next.sdk_models import discover_models, validate_profile


def test_runtime_override_keeps_isolated_home_and_helpers(tmp_path, monkeypatch):
    native = tmp_path / "native"
    (native / "codex-path").mkdir(parents=True)
    monkeypatch.setattr(codex_runtime, "runtime_identity", lambda: {
        "directory": str(native), "binary_path": str(native / "bin/codex.exe")})
    monkeypatch.setenv("PATH", "system-tools")
    original = {"CODEX_HOME": str(tmp_path / "app-home"), "Path": "caller-tools"}
    options = codex_runtime.runtime_launch_options(original)
    assert options["codex_bin"] == str(native / "bin/codex.exe")
    assert options["env"]["CODEX_HOME"] == original["CODEX_HOME"]
    assert len([key for key in options["env"] if key.upper() == "PATH"]) == 1
    assert next(value for key, value in options["env"].items() if key.upper() == "PATH").endswith("caller-tools")
    assert original["Path"] == "caller-tools"
    assert set(key.upper() for key in options["env"]) == {"CODEX_HOME", "PATH"}


def test_missing_packaged_runtime_cannot_silently_use_old_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(codex_runtime.sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(FileNotFoundError):
        codex_runtime.runtime_launch_options({})


def model(name="gpt-6-astra", effort="max", hidden=False):
    return {"id": name, "model": name, "displayName": name, "hidden": hidden,
            "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": "fixture"}]}


def test_catalog_collects_all_pages_and_excludes_hidden_models():
    calls = []

    async def first(**kwargs):
        return {"data": [model("old-model")], "nextCursor": "page-2"}

    async def request(method, params, **kwargs):
        calls.append((method, params))
        return {"data": [model(), model("hidden-model", hidden=True)], "nextCursor": None}

    fake = SimpleNamespace(models=first, _client=SimpleNamespace(request=request))
    catalog = asyncio.run(sdk_model_catalog.read_sdk_models(fake))
    assert {item["model"] for item in catalog} == {"gpt-6-astra", "old-model"}
    assert calls == [("model/list", {"includeHidden": False, "cursor": "page-2"})]


def test_catalog_never_silently_truncates_when_sdk_cannot_paginate():
    fake = SimpleNamespace(models=lambda **kwargs: {"data": [model()], "nextCursor": "next"})
    with pytest.raises(RuntimeError, match="pagination"):
        asyncio.run(sdk_model_catalog.read_sdk_models(fake))


@pytest.mark.parametrize("effort", ["max", "ultra", "future_sdk_level"])
def test_runtime_reasoning_levels_survive_normalization_and_dispatch(effort):
    from unified_paper_orchestrator.writing_sdk_dispatch import WritingSDKDispatcher
    from unified_paper_orchestrator.figure_sdk_dispatch import FigureSDKDispatcher
    from unified_paper_orchestrator.journal_author_sdk_dispatch import JournalAuthorSDKDispatcher
    catalog = normalize_model_catalog([model(effort=effort)])
    assert catalog[0]["supported_reasoning_efforts"] == [effort]
    profile = normalize_execution_profile({"model": "gpt-6-astra", "reasoning_effort": effort}, allow_inherit=False)
    for build_kwargs in (WritingSDKDispatcher._turn_execution_kwargs,
                         FigureSDKDispatcher._turn_execution_kwargs,
                         JournalAuthorSDKDispatcher._turn_kwargs):
        kwargs = build_kwargs(profile)
        assert kwargs["model"] == "gpt-6-astra"
        assert kwargs["effort"].value == effort


@pytest.mark.parametrize("age,version,expected", [(0, "0.153.4", "fresh"),
    (7200, "0.153.4", "stale"), (0, "0.137.0", "stale"), (-300, "0.153.4", "stale")])
def test_provenance_checks_cache_age_and_runtime_without_identity(tmp_path, monkeypatch, age, version, expected):
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(sdk_model_catalog, "runtime_identity", lambda: {"version": "0.153.4"})
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "fetched_at": (now - timedelta(seconds=age)).isoformat(), "client_version": version,
        "email": "private@example.test", "models": []}), encoding="utf-8")
    result = sdk_model_catalog.catalog_provenance(tmp_path, now=now)
    assert result["cache_state"] == expected
    assert result["runtime_version"] == "0.153.4"
    assert "private@example.test" not in json.dumps(result)


def test_unreadable_cache_is_not_misrepresented_as_fresh(tmp_path):
    (tmp_path / "models_cache.json").write_text("broken", encoding="utf-8")
    assert sdk_model_catalog.catalog_provenance(tmp_path)["cache_state"] == "unknown"


@pytest.mark.parametrize("cache_state", ["stale", "unknown"])
def test_stale_catalog_shows_warning_and_blocks_save(cache_state):
    fake = SimpleNamespace(stage_available_models=lambda stage: {"ok": True,
        "models": normalize_model_catalog([model()]), "configured_model": "gpt-5.5",
        "catalog": {"cache_state": cache_state, "runtime_version": "0.153.4"}})
    result = discover_models(fake, "writing")
    assert result["state"] == "stale"
    assert result["configuredModel"] == "gpt-5.5"
    with pytest.raises(RuntimeError):
        validate_profile(fake, "writing", "gpt-6-astra", "max")
