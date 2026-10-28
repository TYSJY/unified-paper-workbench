from __future__ import annotations

from pathlib import Path

from PIL import Image

from unified_paper_orchestrator.ui.main_window import (
    compact_paper_label,
    table_issue_summary,
)
from unified_paper_orchestrator.ui.visual_assets import asset_path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (
    ROOT / "unified_paper_orchestrator" / "ui" / "main_window.py"
).read_text(encoding="utf-8")
THEME_SOURCE = (
    ROOT / "unified_paper_orchestrator" / "ui" / "theme.py"
).read_text(encoding="utf-8")


def test_brand_assets_are_packagable_and_have_expected_geometry() -> None:
    assets = ROOT / "unified_paper_orchestrator" / "ui" / "assets"
    expected_sizes = (24, 32, 40, 48, 64, 96, 128, 256)

    app_icon = assets / "app_icon.png"
    windows_icon = assets / "app_icon.ico"
    brand_mark = assets / "brand_mark.png"
    assert app_icon.is_file()
    assert windows_icon.is_file()
    assert brand_mark.is_file()
    assert windows_icon.stat().st_size > 10_000

    with Image.open(app_icon) as image:
        assert image.size == (512, 512)
        assert image.mode == "RGBA"
    with Image.open(brand_mark) as image:
        assert image.size[0] == image.size[1]
        assert image.mode == "RGBA"
    for size in expected_sizes:
        with Image.open(assets / f"brand_mark_{size}.png") as image:
            assert image.size == (size, size)
            assert image.mode == "RGBA"


def test_asset_resolver_points_at_the_source_asset_when_not_bundled() -> None:
    resolved = asset_path("app_icon.ico")
    assert resolved.is_file()
    assert resolved.name == "app_icon.ico"
    assert resolved.parent.name == "assets"


def test_default_tables_hide_raw_sdk_diagnostics_but_keep_actionable_meaning() -> None:
    token_message = "Step 8 consumed 4173396 tokens; hard limit is 4000000"
    contract_message = (
        "Step 2 output failed its tuned-prompt semantic contract: "
        "reference_bib_count_insufficient"
    )

    assert table_issue_summary(token_message) == (
        "本 Step Token 超出上限；请选择任务查看处理建议"
    )
    assert table_issue_summary(contract_message) == (
        "本 Step 产物未通过验收；请选择任务查看具体原因"
    )
    assert "semantic contract" not in table_issue_summary(contract_message)


def test_compact_paper_label_preserves_identity_prefix_without_table_overflow() -> None:
    paper_id = "2026-07-18_001_contention-aware-kv-precision"
    label = compact_paper_label(paper_id, limit=31)

    assert label.startswith("2026-07-18_001_")
    assert label.endswith("…")
    assert len(label) == 31
    assert compact_paper_label("short-paper", limit=31) == "short-paper"


def test_primary_navigation_and_submission_channels_use_product_styles() -> None:
    assert 'style="Content.TNotebook"' in MAIN_SOURCE
    assert 'style="Nav.TButton"' in MAIN_SOURCE
    assert 'style="Channel.TButton"' in MAIN_SOURCE
    assert "install_window_icon(self)" in MAIN_SOURCE
    assert "UIIconLibrary(self)" in MAIN_SOURCE
    assert "load_brand_mark(self, 44)" in MAIN_SOURCE
    assert 'style.layout("Content.TNotebook.Tab", [])' in THEME_SOURCE
    assert 'style.configure("NavLine.TFrame", background=tokens.accent)' in THEME_SOURCE
