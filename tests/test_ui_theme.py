from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from unified_paper_orchestrator.ui.theme import THEME, apply_enterprise_theme


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (
    ROOT / "unified_paper_orchestrator" / "ui" / "main_window.py"
).read_text(encoding="utf-8")


def _relative_luminance(hex_color: str) -> float:
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]

    def linearize(channel: float) -> float:
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linearize(channel) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: str, background: str) -> float:
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def test_semantic_theme_tokens_keep_readable_contrast() -> None:
    assert _contrast_ratio(THEME.text, THEME.surface) >= 4.5
    assert _contrast_ratio(THEME.text_muted, THEME.surface) >= 4.5
    assert _contrast_ratio("#ffffff", THEME.accent) >= 4.5
    assert _contrast_ratio("#ffffff", THEME.danger) >= 4.5


def test_main_window_applies_one_theme_and_reserves_colour_for_real_status() -> None:
    assert "apply_enterprise_theme(self)" in MAIN_SOURCE
    assert 'text="大一统论文智能工作台"' in MAIN_SOURCE
    assert 'style="AppTitle.TLabel"' in MAIN_SOURCE
    assert 'tree.tag_configure("ready", background=THEME.surface)' in MAIN_SOURCE
    assert 'tree.tag_configure("queued", background=THEME.accent_muted)' in MAIN_SOURCE
    assert 'tree.tag_configure("blocked", background=THEME.danger_muted)' in MAIN_SOURCE
    assert (
        'tree.tag_configure("done", background=THEME.surface, foreground=THEME.success)'
        in MAIN_SOURCE
    )


def test_tk_theme_smoke_exposes_enterprise_styles() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        style = apply_enterprise_theme(root)
        assert style.theme_use() == "clam"
        assert style.lookup("AppTitle.TLabel", "foreground") == THEME.text
        assert style.lookup("Accent.TButton", "background") == THEME.accent
        assert int(style.lookup("Treeview", "rowheight")) >= 28
        assert ttk.Style(root).lookup("Treeview.Heading", "background") == THEME.surface_subtle
    finally:
        root.destroy()
