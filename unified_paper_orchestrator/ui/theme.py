from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from tkinter import ttk
from typing import Final


@dataclass(frozen=True)
class ThemeTokens:
    """Semantic UI tokens for the desktop control panel."""

    canvas: str = "#f6f8fa"
    surface: str = "#ffffff"
    surface_subtle: str = "#f6f8fa"
    surface_hover: str = "#eaeef2"
    surface_pressed: str = "#d8dee4"
    border: str = "#d0d7de"
    border_muted: str = "#d8dee4"
    text: str = "#1f2328"
    text_muted: str = "#57606a"
    text_disabled: str = "#8c959f"
    accent: str = "#0969da"
    accent_hover: str = "#0550ae"
    accent_pressed: str = "#033d8b"
    accent_muted: str = "#ddf4ff"
    success: str = "#1a7f37"
    success_emphasis: str = "#1f883d"
    success_muted: str = "#dafbe1"
    attention: str = "#9a6700"
    attention_emphasis: str = "#9a6700"
    attention_muted: str = "#fff8c5"
    severe: str = "#bc4c00"
    severe_muted: str = "#fff1e5"
    danger: str = "#cf222e"
    danger_hover: str = "#a40e26"
    danger_muted: str = "#ffebe9"
    done: str = "#8250df"
    done_muted: str = "#fbefff"


THEME: Final = ThemeTokens()


def _available_font(root: tk.Misc, *candidates: str) -> str:
    available = {str(name) for name in tkfont.families(root)}
    return next((name for name in candidates if name in available), "TkDefaultFont")


def _configure_named_fonts(root: tk.Misc) -> dict[str, str | int]:
    ui_family = _available_font(root, "Microsoft YaHei UI", "Segoe UI", "Microsoft JhengHei UI")
    mono_family = _available_font(root, "Cascadia Mono", "Consolas", "Courier New")
    # The control panel is a long-running workbench, not a dense database
    # inspector.  Use one extra point on wide/high-DPI displays while keeping
    # 1366px screens compact enough to expose every primary action.
    try:
        base_size = 11 if int(root.winfo_screenwidth()) >= 1600 else 10
    except (AttributeError, tk.TclError, ValueError):
        base_size = 10
    small_size = max(9, base_size - 1)
    for name, size, weight in (
        ("TkDefaultFont", base_size, "normal"),
        ("TkTextFont", base_size, "normal"),
        ("TkMenuFont", base_size, "normal"),
        ("TkHeadingFont", base_size, "bold"),
        ("TkCaptionFont", base_size, "normal"),
        ("TkSmallCaptionFont", small_size, "normal"),
        ("TkIconFont", base_size, "normal"),
        ("TkTooltipFont", small_size, "normal"),
    ):
        try:
            tkfont.nametofont(name, root=root).configure(
                family=ui_family,
                size=size,
                weight=weight,
            )
        except tk.TclError:
            continue
    try:
        tkfont.nametofont("TkFixedFont", root=root).configure(
            family=mono_family,
            size=base_size,
        )
    except tk.TclError:
        pass
    return {
        "ui": ui_family,
        "mono": mono_family,
        "base_size": base_size,
        "small_size": small_size,
    }


def apply_enterprise_theme(root: tk.Misc) -> ttk.Style:
    """Apply a restrained GitHub/Fluent-inspired light theme to one Tk app."""

    fonts = _configure_named_fonts(root)
    ui_family = str(fonts["ui"])
    base_size = int(fonts["base_size"])
    small_size = int(fonts["small_size"])
    tokens = THEME
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    root.option_add("*Font", (ui_family, base_size))
    root.option_add("*Menu.Font", (ui_family, base_size))
    root.option_add("*Menu.background", tokens.surface)
    root.option_add("*Menu.foreground", tokens.text)
    root.option_add("*Menu.activeBackground", tokens.accent_muted)
    root.option_add("*Menu.activeForeground", tokens.text)
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Text.background", tokens.surface)
    root.option_add("*Text.foreground", tokens.text)
    root.option_add("*Text.insertBackground", tokens.text)
    root.option_add("*Text.selectBackground", tokens.accent_muted)
    root.option_add("*Text.selectForeground", tokens.text)
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightBackground", tokens.border)
    root.option_add("*Text.highlightColor", tokens.accent)

    style.configure(".", font=(ui_family, base_size))
    style.configure("TFrame", background=tokens.surface)
    style.configure("App.TFrame", background=tokens.canvas)
    style.configure("Header.TFrame", background=tokens.surface)
    style.configure("Brand.TFrame", background=tokens.surface)
    style.configure("BrandTile.TFrame", background="#eef6ff")
    style.configure("CommandBar.TFrame", background=tokens.surface)
    style.configure("Workspace.TFrame", background=tokens.surface_subtle)
    style.configure("StatusBar.TFrame", background=tokens.surface_subtle)

    style.configure("TLabel", background=tokens.surface, foreground=tokens.text)
    style.configure(
        "AppTitle.TLabel",
        background=tokens.surface,
        foreground=tokens.text,
        font=(ui_family, base_size + 6, "bold"),
    )
    style.configure(
        "AppSubtitle.TLabel",
        background=tokens.surface,
        foreground=tokens.text_muted,
        font=(ui_family, base_size),
    )
    style.configure(
        "Badge.TLabel",
        background=tokens.accent_muted,
        foreground=tokens.accent_hover,
        font=(ui_family, small_size, "bold"),
        padding=(8, 3),
    )
    style.configure(
        "BrandMark.TLabel",
        background="#eef6ff",
        foreground=tokens.accent,
        padding=(3, 3),
    )
    style.configure(
        "Breadcrumb.TLabel",
        background=tokens.surface_subtle,
        foreground=tokens.text_muted,
        font=(ui_family, small_size),
    )
    style.configure(
        "BreadcrumbStrong.TLabel",
        background=tokens.surface_subtle,
        foreground=tokens.text,
        font=(ui_family, small_size, "bold"),
    )
    style.configure(
        "Workspace.TLabel",
        background=tokens.surface_subtle,
        foreground=tokens.text_muted,
    )
    style.configure(
        "Status.TLabel",
        background=tokens.surface_subtle,
        foreground=tokens.text_muted,
        padding=(2, 1),
    )
    style.configure(
        "Muted.TLabel",
        background=tokens.surface,
        foreground=tokens.text_muted,
    )
    style.configure(
        "PageTitle.TLabel",
        background=tokens.surface,
        foreground=tokens.text,
        font=(ui_family, base_size + 4, "bold"),
    )
    style.configure(
        "SectionTitle.TLabel",
        background=tokens.surface,
        foreground=tokens.text,
        font=(ui_family, base_size + 1, "bold"),
    )
    style.configure(
        "SectionMeta.TLabel",
        background=tokens.surface,
        foreground=tokens.text_muted,
        font=(ui_family, small_size),
    )
    style.configure(
        "Card.TFrame",
        background=tokens.surface,
        bordercolor=tokens.border,
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "MetricCard.TFrame",
        background=tokens.surface,
        bordercolor="#dbe4ee",
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "DetailCard.TFrame",
        background=tokens.surface,
        bordercolor="#dbe4ee",
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "CardLabel.TLabel",
        background=tokens.surface,
        foreground=tokens.text_muted,
        font=(ui_family, small_size),
    )
    style.configure(
        "CardValue.TLabel",
        background=tokens.surface,
        foreground=tokens.text,
        font=(ui_family, base_size + 8, "bold"),
    )
    for style_name, background, foreground in (
        ("NeutralBadge.TLabel", tokens.surface_subtle, tokens.text_muted),
        ("InfoBadge.TLabel", tokens.accent_muted, tokens.accent_hover),
        ("SuccessBadge.TLabel", tokens.success_muted, tokens.success),
        ("AttentionBadge.TLabel", tokens.attention_muted, tokens.attention),
        ("DangerBadge.TLabel", tokens.danger_muted, tokens.danger),
        ("SevereBadge.TLabel", tokens.severe_muted, tokens.severe),
    ):
        style.configure(
            style_name,
            background=background,
            foreground=foreground,
            font=(ui_family, small_size, "bold"),
            padding=(8, 3),
        )
    for style_name, foreground in (
        ("InfoText.TLabel", tokens.accent_hover),
        ("SuccessText.TLabel", tokens.success),
        ("AttentionText.TLabel", tokens.attention),
        ("DangerText.TLabel", tokens.danger),
        ("SevereText.TLabel", tokens.severe),
    ):
        style.configure(
            style_name,
            background=tokens.surface,
            foreground=foreground,
            font=(ui_family, base_size),
        )

    style.configure(
        "TLabelframe",
        background=tokens.surface,
        bordercolor=tokens.border,
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "TLabelframe.Label",
        background=tokens.surface,
        foreground=tokens.text_muted,
        font=(ui_family, base_size, "bold"),
        padding=(2, 0),
    )

    button_options = {
        "background": tokens.surface_subtle,
        "foreground": tokens.text,
        "bordercolor": tokens.border,
        "lightcolor": tokens.surface_subtle,
        "darkcolor": tokens.surface_subtle,
        "focuscolor": tokens.accent,
        "focusthickness": 1,
        "borderwidth": 1,
        "relief": "flat",
        "padding": (12, 8),
    }
    style.configure("TButton", **button_options)
    style.map(
        "TButton",
        background=[("pressed", tokens.surface_pressed), ("active", tokens.surface_hover)],
        foreground=[("disabled", tokens.text_disabled)],
        bordercolor=[("focus", tokens.accent), ("active", tokens.border)],
    )
    style.configure(
        "Accent.TButton",
        background=tokens.accent,
        foreground="#ffffff",
        bordercolor=tokens.accent,
        lightcolor=tokens.accent,
        darkcolor=tokens.accent,
        padding=(12, 7),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("pressed", tokens.accent_pressed), ("active", tokens.accent_hover)],
        foreground=[("disabled", "#ffffff")],
        bordercolor=[("pressed", tokens.accent_pressed), ("active", tokens.accent_hover)],
    )
    style.configure(
        "Danger.TButton",
        background=tokens.danger,
        foreground="#ffffff",
        bordercolor=tokens.danger,
        lightcolor=tokens.danger,
        darkcolor=tokens.danger,
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Danger.TButton",
        background=[("pressed", "#82071e"), ("active", tokens.danger_hover)],
        bordercolor=[("pressed", "#82071e"), ("active", tokens.danger_hover)],
    )
    style.configure(
        "Warning.TButton",
        background=tokens.attention_muted,
        foreground=tokens.attention,
        bordercolor="#d4a72c",
        lightcolor=tokens.attention_muted,
        darkcolor=tokens.attention_muted,
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Warning.TButton",
        background=[("pressed", "#f2cc60"), ("active", "#fff1b3")],
    )
    style.configure(
        "Quiet.TButton",
        background=tokens.surface,
        foreground=tokens.text_muted,
        bordercolor=tokens.surface,
        lightcolor=tokens.surface,
        darkcolor=tokens.surface,
    )
    style.map(
        "Quiet.TButton",
        background=[("pressed", tokens.surface_pressed), ("active", tokens.surface_hover)],
        foreground=[("active", tokens.text)],
        bordercolor=[("focus", tokens.accent), ("active", tokens.surface_hover)],
    )

    style.configure(
        "TMenubutton",
        background=tokens.surface_subtle,
        foreground=tokens.text,
        bordercolor=tokens.border,
        lightcolor=tokens.surface_subtle,
        darkcolor=tokens.surface_subtle,
        borderwidth=1,
        relief="flat",
        padding=(11, 7),
        arrowsize=12,
    )
    style.map(
        "TMenubutton",
        background=[("pressed", tokens.surface_pressed), ("active", tokens.surface_hover)],
        bordercolor=[("focus", tokens.accent)],
    )

    style.configure("TNotebook", background=tokens.canvas, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background=tokens.canvas,
        foreground=tokens.text_muted,
        borderwidth=0,
        padding=(18, 11),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", tokens.surface), ("active", tokens.surface_hover)],
        foreground=[("selected", tokens.accent), ("active", tokens.text)],
    )
    style.configure(
        "Navigation.TNotebook",
        background=tokens.surface,
        bordercolor=tokens.border_muted,
        borderwidth=1,
        tabmargins=(10, 5, 10, 0),
    )
    style.configure(
        "Navigation.TNotebook.Tab",
        background=tokens.surface,
        foreground=tokens.text_muted,
        borderwidth=0,
        padding=(18, 12),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Navigation.TNotebook.Tab",
        background=[("selected", tokens.surface), ("active", tokens.surface_subtle)],
        foreground=[("selected", tokens.accent), ("active", tokens.text)],
        lightcolor=[("selected", tokens.accent)],
        bordercolor=[("selected", tokens.accent)],
    )
    style.configure(
        "Content.TNotebook",
        background=tokens.surface,
        bordercolor=tokens.border_muted,
        borderwidth=1,
        tabmargins=(0, 0, 0, 0),
    )
    # Primary navigation is rendered by the dedicated navigation bar.  The
    # notebook remains the stable page container and keyboard/state owner.
    style.layout("Content.TNotebook.Tab", [])
    style.configure(
        "Nav.TButton",
        background=tokens.surface,
        foreground=tokens.text_muted,
        bordercolor=tokens.surface,
        lightcolor=tokens.surface,
        darkcolor=tokens.surface,
        borderwidth=0,
        relief="flat",
        padding=(16, 10),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Nav.TButton",
        background=[("selected", tokens.surface), ("active", tokens.surface_subtle)],
        foreground=[("selected", tokens.accent), ("active", tokens.text)],
        bordercolor=[("focus", tokens.surface)],
    )
    style.configure("NavLine.TFrame", background=tokens.accent)
    style.configure(
        "Channel.TButton",
        background=tokens.surface,
        foreground=tokens.text,
        bordercolor=tokens.border,
        lightcolor=tokens.surface,
        darkcolor=tokens.surface,
        borderwidth=1,
        relief="flat",
        padding=(16, 10),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Channel.TButton",
        background=[("selected", tokens.accent_muted), ("active", tokens.surface_subtle)],
        foreground=[("selected", tokens.accent), ("active", tokens.text)],
        bordercolor=[("selected", tokens.accent), ("focus", tokens.accent)],
    )
    style.configure(
        "Detail.TNotebook",
        background=tokens.surface,
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "Detail.TNotebook.Tab",
        background=tokens.surface_subtle,
        foreground=tokens.text_muted,
        borderwidth=0,
        padding=(12, 8),
        font=(ui_family, small_size, "bold"),
    )
    style.map(
        "Detail.TNotebook.Tab",
        background=[("selected", tokens.surface), ("active", tokens.surface_hover)],
        foreground=[("selected", tokens.accent), ("active", tokens.text)],
    )

    style.configure(
        "Treeview",
        background=tokens.surface,
        fieldbackground=tokens.surface,
        foreground=tokens.text,
        bordercolor=tokens.border,
        lightcolor=tokens.border,
        darkcolor=tokens.border,
        borderwidth=1,
        relief="flat",
        rowheight=36 if base_size >= 11 else 33,
        font=(ui_family, base_size),
    )
    style.map(
        "Treeview",
        background=[("selected", tokens.accent_muted)],
        foreground=[("selected", tokens.text)],
    )
    style.configure(
        "Treeview.Heading",
        background=tokens.surface_subtle,
        foreground=tokens.text_muted,
        bordercolor=tokens.border,
        lightcolor=tokens.surface_subtle,
        darkcolor=tokens.surface_subtle,
        relief="flat",
        padding=(10, 9),
        font=(ui_family, base_size, "bold"),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", tokens.surface_hover)],
        foreground=[("active", tokens.text)],
    )

    for style_name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            style_name,
            fieldbackground=tokens.surface,
            foreground=tokens.text,
            bordercolor=tokens.border,
            lightcolor=tokens.surface,
            darkcolor=tokens.surface,
            insertcolor=tokens.text,
            padding=(8, 6),
        )
        style.map(
            style_name,
            bordercolor=[("focus", tokens.accent)],
            fieldbackground=[("disabled", tokens.surface_subtle)],
            foreground=[("disabled", tokens.text_disabled)],
        )
    style.configure(
        "TCheckbutton",
        background=tokens.surface,
        foreground=tokens.text,
        focuscolor=tokens.accent,
        padding=(2, 3),
    )
    style.map("TCheckbutton", background=[("active", tokens.surface)])
    style.configure("TRadiobutton", background=tokens.surface, foreground=tokens.text, padding=(2, 3))
    style.map("TRadiobutton", background=[("active", tokens.surface)])

    style.configure(
        "Vertical.TScrollbar",
        background=tokens.border_muted,
        troughcolor=tokens.surface_subtle,
        bordercolor=tokens.surface_subtle,
        arrowcolor=tokens.text_muted,
        relief="flat",
        width=12,
    )
    style.configure(
        "Horizontal.TScrollbar",
        background=tokens.border_muted,
        troughcolor=tokens.surface_subtle,
        bordercolor=tokens.surface_subtle,
        arrowcolor=tokens.text_muted,
        relief="flat",
        width=12,
    )
    style.configure("TPanedwindow", background=tokens.border, sashwidth=5)
    style.configure("TSeparator", background=tokens.border_muted)
    style.configure(
        "Horizontal.TProgressbar",
        background=tokens.accent,
        troughcolor=tokens.surface_pressed,
        bordercolor=tokens.surface_pressed,
        lightcolor=tokens.accent,
        darkcolor=tokens.accent,
    )
    return style


def configure_text_widget(widget: tk.Text, *, readonly: bool = False) -> None:
    """Apply theme tokens to classic Tk Text widgets used beside ttk controls."""

    widget.configure(
        background=THEME.surface,
        foreground=THEME.text,
        insertbackground=THEME.text,
        selectbackground=THEME.accent_muted,
        selectforeground=THEME.text,
        highlightthickness=1,
        highlightbackground=THEME.border,
        highlightcolor=THEME.accent,
        relief="flat",
        padx=9,
        pady=7,
    )
    if readonly:
        widget.configure(cursor="arrow")
