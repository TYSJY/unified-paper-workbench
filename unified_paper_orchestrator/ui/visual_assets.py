from __future__ import annotations

import math
import sys
import tkinter as tk
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageTk

from .theme import THEME


_ASSET_SUBDIR: Final = Path("unified_paper_orchestrator") / "ui" / "assets"


def asset_path(filename: str) -> Path:
    """Resolve one bundled UI asset in source and PyInstaller builds."""

    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        bundled = Path(str(bundle_root)) / _ASSET_SUBDIR / filename
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parent / "assets" / filename


def install_window_icon(window: tk.Tk | tk.Toplevel) -> list[tk.PhotoImage]:
    """Install the branded title-bar/taskbar icon and retain Tk references."""

    retained: list[tk.PhotoImage] = []
    try:
        icon = tk.PhotoImage(master=window, file=str(asset_path("app_icon.png")))
        window.iconphoto(True, icon)
        retained.append(icon)
    except (OSError, tk.TclError):
        pass
    try:
        window.iconbitmap(default=str(asset_path("app_icon.ico")))
    except (OSError, tk.TclError):
        pass
    return retained


def load_brand_mark(master: tk.Misc, size: int = 42) -> ImageTk.PhotoImage:
    source = Image.open(asset_path("brand_mark.png")).convert("RGBA")
    resized = source.resize((int(size), int(size)), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(resized, master=master)


_TONE_COLORS: Final = {
    "neutral": (THEME.text_muted, "#eef1f4"),
    "info": (THEME.accent, "#e8f2ff"),
    "success": (THEME.success, "#e6f6ea"),
    "attention": (THEME.attention, "#fff3cf"),
    "severe": (THEME.severe, "#fff0e5"),
    "danger": (THEME.danger, "#ffebe9"),
    "done": (THEME.done, "#f2eafe"),
    "inverse": ("#ffffff", THEME.accent),
}


class UIIconLibrary:
    """Small code-native icons with one visual language and no font dependency."""

    def __init__(self, master: tk.Misc) -> None:
        self.master = master
        self._cache: dict[tuple[str, int, str, bool], ImageTk.PhotoImage] = {}

    def icon(
        self,
        name: str,
        *,
        size: int = 16,
        tone: str = "neutral",
        disc: bool = False,
    ) -> ImageTk.PhotoImage:
        key = (str(name), int(size), str(tone), bool(disc))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        foreground, background = _TONE_COLORS.get(tone, _TONE_COLORS["neutral"])
        scale = 4
        pixels = max(8, int(size)) * scale
        image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        if disc:
            inset = max(1, round(pixels * 0.025))
            draw.ellipse((inset, inset, pixels - inset, pixels - inset), fill=background)
            icon_box = (
                round(pixels * 0.26),
                round(pixels * 0.26),
                round(pixels * 0.74),
                round(pixels * 0.74),
            )
        else:
            icon_box = (
                round(pixels * 0.13),
                round(pixels * 0.13),
                round(pixels * 0.87),
                round(pixels * 0.87),
            )
        self._draw_symbol(draw, str(name), icon_box, foreground)
        image = image.resize((int(size), int(size)), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=self.master)
        self._cache[key] = photo
        return photo

    @staticmethod
    def _draw_symbol(
        draw: ImageDraw.ImageDraw,
        name: str,
        box: tuple[int, int, int, int],
        color: str,
    ) -> None:
        x0, y0, x1, y1 = box
        width = max(2, round((x1 - x0) * 0.11))
        mid_x = (x0 + x1) // 2
        mid_y = (y0 + y1) // 2
        radius = max(2, round((x1 - x0) * 0.10))

        if name in {"overview", "grid"}:
            gap = max(width, round((x1 - x0) * 0.13))
            cell_w = (x1 - x0 - gap) // 2
            cell_h = (y1 - y0 - gap) // 2
            for row in range(2):
                for column in range(2):
                    left = x0 + column * (cell_w + gap)
                    top = y0 + row * (cell_h + gap)
                    draw.rounded_rectangle(
                        (left, top, left + cell_w, top + cell_h),
                        radius=max(1, width),
                        outline=color,
                        width=width,
                    )
            return
        if name in {"writing", "document", "total"}:
            fold = round((x1 - x0) * 0.27)
            draw.rounded_rectangle((x0, y0, x1, y1), radius=width, outline=color, width=width)
            draw.line((x1 - fold, y0, x1 - fold, y0 + fold, x1, y0 + fold), fill=color, width=width)
            for offset in (0.48, 0.66, 0.82):
                y = round(y0 + (y1 - y0) * offset)
                draw.line((x0 + width * 2, y, x1 - width * 2, y), fill=color, width=width)
            return
        if name in {"figures", "image", "chart"}:
            draw.rounded_rectangle((x0, y0, x1, y1), radius=width, outline=color, width=width)
            draw.ellipse(
                (x0 + width * 2, y0 + width * 2, x0 + width * 2 + radius * 2, y0 + width * 2 + radius * 2),
                fill=color,
            )
            draw.line(
                (
                    x0 + width,
                    y1 - width * 2,
                    x0 + round((x1 - x0) * 0.38),
                    mid_y,
                    mid_x,
                    y1 - round((y1 - y0) * 0.34),
                    x1 - width,
                    y1 - width * 2,
                ),
                fill=color,
                width=width,
                joint="curve",
            )
            return
        if name in {"authors", "account", "human"}:
            head_r = round((x1 - x0) * 0.16)
            draw.ellipse((mid_x - head_r, y0, mid_x + head_r, y0 + head_r * 2), outline=color, width=width)
            draw.arc((x0, mid_y, x1, y1 + round((y1 - y0) * 0.28)), 190, 350, fill=color, width=width)
            if name == "authors":
                small = max(2, head_r - width)
                draw.ellipse((x0, mid_y - small * 2, x0 + small * 2, mid_y), outline=color, width=width)
                draw.ellipse((x1 - small * 2, mid_y - small * 2, x1, mid_y), outline=color, width=width)
            return
        if name in {"submission", "inbox"}:
            tray_y = y0 + round((y1 - y0) * 0.58)
            draw.rounded_rectangle((x0, tray_y, x1, y1), radius=width, outline=color, width=width)
            draw.line((mid_x, y0, mid_x, tray_y + width), fill=color, width=width)
            draw.line((mid_x, y0, x0 + round((x1 - x0) * 0.30), y0 + round((y1 - y0) * 0.24)), fill=color, width=width)
            draw.line((mid_x, y0, x1 - round((x1 - x0) * 0.30), y0 + round((y1 - y0) * 0.24)), fill=color, width=width)
            return
        if name == "refresh":
            inset = width
            draw.arc((x0 + inset, y0 + inset, x1 - inset, y1 - inset), 35, 315, fill=color, width=width)
            angle = math.radians(35)
            px = mid_x + math.cos(angle) * (x1 - x0) * 0.38
            py = mid_y - math.sin(angle) * (y1 - y0) * 0.38
            draw.polygon(
                ((px, py), (px - width * 3, py - width), (px - width, py + width * 3)),
                fill=color,
            )
            return
        if name == "pipeline":
            quarter = round((x1 - x0) * 0.24)
            draw.line((x0 + quarter, mid_y, x1 - quarter, mid_y), fill=color, width=width)
            for cx in (x0 + quarter, mid_x, x1 - quarter):
                draw.polygon(
                    ((cx, mid_y - radius * 2), (cx + radius * 2, mid_y), (cx, mid_y + radius * 2), (cx - radius * 2, mid_y)),
                    fill=color,
                )
            return
        if name in {"add", "plus"}:
            draw.line((mid_x, y0, mid_x, y1), fill=color, width=width)
            draw.line((x0, mid_y, x1, mid_y), fill=color, width=width)
            return
        if name in {"more", "ellipsis"}:
            for cx in (x0 + radius, mid_x, x1 - radius):
                draw.ellipse((cx - radius, mid_y - radius, cx + radius, mid_y + radius), fill=color)
            return
        if name == "stop":
            draw.rounded_rectangle((x0, y0, x1, y1), radius=width, fill=color)
            return
        if name in {"active", "play"}:
            draw.polygon(((x0, y0), (x1, mid_y), (x0, y1)), fill=color)
            return
        if name in {"waiting", "clock"}:
            draw.ellipse((x0, y0, x1, y1), outline=color, width=width)
            draw.line((mid_x, mid_y, mid_x, y0 + width * 2), fill=color, width=width)
            draw.line((mid_x, mid_y, x1 - width * 2, mid_y + width), fill=color, width=width)
            return
        if name in {"complete", "check"}:
            draw.ellipse((x0, y0, x1, y1), outline=color, width=width)
            draw.line((x0 + width * 2, mid_y, mid_x - width, y1 - width * 2, x1 - width * 2, y0 + width * 2), fill=color, width=width, joint="curve")
            return
        if name in {"agent", "robot"}:
            draw.rounded_rectangle((x0, y0 + width * 2, x1, y1), radius=width * 2, outline=color, width=width)
            draw.line((mid_x, y0, mid_x, y0 + width * 2), fill=color, width=width)
            draw.ellipse((mid_x - radius, y0 - radius, mid_x + radius, y0 + radius), fill=color)
            eye_y = mid_y
            draw.ellipse((x0 + width * 2, eye_y - radius, x0 + width * 2 + radius * 2, eye_y + radius), fill=color)
            draw.ellipse((x1 - width * 2 - radius * 2, eye_y - radius, x1 - width * 2, eye_y + radius), fill=color)
            return

        draw.ellipse((x0, y0, x1, y1), outline=color, width=width)


__all__ = ["UIIconLibrary", "asset_path", "install_window_icon", "load_brand_mark"]
