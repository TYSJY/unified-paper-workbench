from __future__ import annotations

import tkinter as tk
from collections.abc import Iterable, Mapping, Sequence
from tkinter import ttk
from typing import Any

from .theme import THEME, configure_text_widget


_TONE_STYLES = {
    "neutral": "NeutralBadge.TLabel",
    "info": "InfoBadge.TLabel",
    "success": "SuccessBadge.TLabel",
    "attention": "AttentionBadge.TLabel",
    "danger": "DangerBadge.TLabel",
    "severe": "SevereBadge.TLabel",
}


class MetricStrip(ttk.Frame):
    """A stable, low-cost summary row shared by workflow pages."""

    def __init__(
        self,
        parent: tk.Misc,
        specs: Sequence[tuple[str, str]],
        *,
        compact: bool = False,
        icons: Mapping[str, tk.PhotoImage] | None = None,
    ) -> None:
        super().__init__(parent)
        self._compact = bool(compact)
        self._icon_refs = dict(icons or {})
        self._values: dict[str, tk.StringVar] = {}
        self._meta: dict[str, tk.StringVar] = {}
        self._cards: list[ttk.Frame] = []
        self._layout_columns = 0
        for column, (key, label) in enumerate(specs):
            self.columnconfigure(column, weight=1, uniform="metric")
            card = ttk.Frame(
                self,
                style="MetricCard.TFrame",
                padding=(11, 8) if self._compact else (13, 11),
            )
            card.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 5, 0 if column == len(specs) - 1 else 5),
            )
            image = self._icon_refs.get(key)
            content_column = 1 if image is not None else 0
            card.columnconfigure(content_column, weight=1)
            if image is not None:
                ttk.Label(card, image=image).grid(
                    row=0,
                    column=0,
                    rowspan=3,
                    sticky="w",
                    padx=(0, 11),
                )
            ttk.Label(card, text=label, style="CardLabel.TLabel").grid(
                row=0,
                column=content_column,
                sticky="w",
            )
            value_var = tk.StringVar(value="0")
            meta_var = tk.StringVar(value="")
            ttk.Label(card, textvariable=value_var, style="CardValue.TLabel").grid(
                row=1,
                column=content_column,
                sticky="w",
                pady=(1 if self._compact else 3, 0),
            )
            ttk.Label(card, textvariable=meta_var, style="SectionMeta.TLabel").grid(
                row=2,
                column=content_column,
                sticky="w",
                pady=(1, 0),
            )
            self._values[key] = value_var
            self._meta[key] = meta_var
            self._cards.append(card)
        self.bind("<Configure>", self._reflow_cards, add="+")
        self.after_idle(self._reflow_cards)

    def _reflow_cards(self, event: tk.Event | None = None) -> None:
        width = int(getattr(event, "width", 0) or self.winfo_width() or 0)
        count = len(self._cards)
        if not count:
            return
        if width >= 920:
            columns = count
        elif width >= 600:
            columns = min(3, count)
        else:
            columns = min(2, count)
        if columns == self._layout_columns:
            return
        self._layout_columns = columns
        for column in range(count):
            self.columnconfigure(column, weight=0, uniform="")
        for column in range(columns):
            self.columnconfigure(column, weight=1, uniform="metric")
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            card.grid_configure(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 5, 0 if column == columns - 1 else 5),
                pady=(0 if row == 0 else 5, 0),
            )

    def update_values(self, values: Mapping[str, Any]) -> None:
        for key, value_var in self._values.items():
            raw = values.get(key, "0")
            if isinstance(raw, tuple):
                value, meta = raw[0], raw[1] if len(raw) > 1 else ""
            else:
                value, meta = raw, ""
            value_var.set(str(value))
            self._meta[key].set(str(meta or ""))


class StructuredDetailPanel(ttk.Frame):
    """A compact human summary with an opt-in technical layer.

    The old control panel rendered every runtime field in one raw text box.
    Keeping the technical text is valuable for diagnosis, but it should not be
    the first thing a user has to parse.  This component gives every stage the
    same information hierarchy without changing any workflow data.
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        empty_title: str = "请选择一篇论文",
        technical_label: str = "技术信息",
        empty_image: tk.PhotoImage | None = None,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._render_signature = ""
        self._wrappable_labels: list[ttk.Label] = []
        self._empty_image = empty_image

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        title_row = ttk.Frame(header)
        title_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        title_row.columnconfigure(0, weight=1)
        self.title_var = tk.StringVar(value=empty_title)
        self.title_label = ttk.Label(
            title_row,
            textvariable=self.title_var,
            style="SectionTitle.TLabel",
            justify=tk.LEFT,
        )
        self.title_label.grid(
            row=0,
            column=0,
            sticky="w",
        )
        self.status_var = tk.StringVar(value="未选择")
        self.status_badge = ttk.Label(
            title_row,
            textvariable=self.status_var,
            style="NeutralBadge.TLabel",
        )
        self.status_badge.grid(row=0, column=1, sticky="e", padx=(10, 0))

        self.show_technical_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            header,
            text=technical_label,
            variable=self.show_technical_var,
            command=self._toggle_layer,
        ).grid(row=1, column=1, sticky="e", padx=(12, 0), pady=(3, 0))

        self.summary_host = ttk.Frame(self, style="Card.TFrame")
        self.summary_host.grid(row=1, column=0, sticky="nsew")
        self.summary_host.columnconfigure(0, weight=1)
        self.summary_host.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self.summary_host,
            background=THEME.surface,
            borderwidth=0,
            highlightthickness=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        summary_scroll = ttk.Scrollbar(
            self.summary_host,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        summary_scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=summary_scroll.set)
        self.summary = ttk.Frame(self.canvas, padding=(12, 10))
        self.summary.columnconfigure(0, weight=1)
        self._summary_window = self.canvas.create_window(
            (0, 0),
            window=self.summary,
            anchor="nw",
        )
        self.summary.bind("<Configure>", self._sync_scroll_region, add="+")
        self.canvas.bind("<Configure>", self._sync_summary_width, add="+")
        self.canvas.bind("<Configure>", self._sync_wraplengths, add="+")
        self.canvas.bind("<MouseWheel>", self._on_mousewheel, add="+")

        self.technical_host = ttk.Frame(self)
        self.technical_host.columnconfigure(0, weight=1)
        self.technical_host.rowconfigure(0, weight=1)
        self.technical_text = tk.Text(
            self.technical_host,
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
        )
        configure_text_widget(self.technical_text, readonly=True)
        self.technical_text.grid(row=0, column=0, sticky="nsew")
        technical_scroll = ttk.Scrollbar(
            self.technical_host,
            orient=tk.VERTICAL,
            command=self.technical_text.yview,
        )
        technical_scroll.grid(row=0, column=1, sticky="ns")
        self.technical_text.configure(yscrollcommand=technical_scroll.set, state="disabled")

        self.render(
            title=empty_title,
            status="未选择",
            tone="neutral",
            headline="从左侧列表选择论文后，这里会显示当前状态、下一步和需要处理的事项。",
            sections=(),
        )

    def _sync_scroll_region(self, _event: tk.Event | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_summary_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._summary_window, width=max(1, int(event.width)))

    def _sync_wraplengths(self, event: tk.Event | None = None) -> None:
        width = int(getattr(event, "width", 0) or self.canvas.winfo_width() or 0)
        wraplength = max(190, width - 76)
        try:
            self.title_label.configure(wraplength=max(170, width - 30))
        except tk.TclError:
            pass
        active: list[ttk.Label] = []
        for label in self._wrappable_labels:
            try:
                if label.winfo_exists():
                    label.configure(wraplength=wraplength)
                    active.append(label)
            except tk.TclError:
                continue
        self._wrappable_labels = active

    def _on_mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _toggle_layer(self) -> None:
        if self.show_technical_var.get():
            self.summary_host.grid_remove()
            self.technical_host.grid(row=1, column=0, sticky="nsew")
        else:
            self.technical_host.grid_remove()
            self.summary_host.grid()

    @staticmethod
    def _clean_fields(fields: Iterable[Sequence[Any]]) -> list[tuple[str, str]]:
        cleaned: list[tuple[str, str]] = []
        for item in fields:
            if len(item) < 2:
                continue
            label = str(item[0] or "").strip()
            value = str(item[1] if item[1] is not None else "-").strip() or "-"
            if label:
                cleaned.append((label, value))
        return cleaned

    def render(
        self,
        *,
        title: str,
        status: str,
        tone: str = "neutral",
        headline: str = "",
        sections: Iterable[Mapping[str, Any]] = (),
        events: Iterable[str] = (),
        technical_text: str | None = None,
    ) -> None:
        normalized_sections = [dict(section) for section in sections]
        normalized_events = [str(event).strip() for event in events if str(event).strip()]
        signature = repr((title, status, tone, headline, normalized_sections, normalized_events))
        self.title_var.set(str(title or "请选择一篇论文"))
        self.status_var.set(str(status or "需核对"))
        self.status_badge.configure(style=_TONE_STYLES.get(tone, "NeutralBadge.TLabel"))
        if technical_text is not None:
            self.set_technical_text(technical_text)
        if signature == self._render_signature:
            return
        self._render_signature = signature

        for child in self.summary.winfo_children():
            child.destroy()
        self._wrappable_labels = []

        row = 0
        is_empty_state = (
            str(status or "").strip() == "未选择"
            and not normalized_sections
            and not normalized_events
        )
        if is_empty_state:
            empty = ttk.Frame(self.summary, padding=(24, 38))
            empty.grid(row=0, column=0, sticky="nsew")
            empty.columnconfigure(0, weight=1)
            if self._empty_image is not None:
                ttk.Label(empty, image=self._empty_image).grid(row=0, column=0, pady=(0, 14))
            ttk.Label(
                empty,
                text=str(title or "请选择一篇论文"),
                style="PageTitle.TLabel",
                anchor=tk.CENTER,
            ).grid(row=1, column=0, sticky="ew")
            empty_headline = ttk.Label(
                empty,
                text=str(headline or "选择左侧论文后即可查看当前状态和下一步。"),
                style="Muted.TLabel",
                justify=tk.CENTER,
            )
            empty_headline.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            self._wrappable_labels.append(empty_headline)
            self.summary.update_idletasks()
            self._sync_wraplengths()
            self._sync_scroll_region()
            self.canvas.yview_moveto(0.0)
            return

        if headline:
            headline_card = ttk.Frame(self.summary, style="DetailCard.TFrame", padding=(13, 11))
            headline_card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            headline_card.columnconfigure(0, weight=1)
            headline_label = ttk.Label(
                headline_card,
                text=str(headline),
                justify=tk.LEFT,
            )
            headline_label.grid(row=0, column=0, sticky="ew")
            self._wrappable_labels.append(headline_label)
            row += 1

        for section in normalized_sections:
            card = ttk.Frame(self.summary, style="DetailCard.TFrame", padding=(13, 11))
            card.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            card.columnconfigure(1, weight=1)
            section_title = str(section.get("title") or "详情")
            section_header = ttk.Frame(card)
            section_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
            ttk.Frame(section_header, style="NavLine.TFrame", width=3, height=16).pack(
                side=tk.LEFT,
                padx=(0, 7),
            )
            ttk.Label(section_header, text=section_title, style="SectionTitle.TLabel").pack(side=tk.LEFT)
            card_row = 1
            for label, value in self._clean_fields(section.get("fields") or ()):
                ttk.Label(card, text=label, style="CardLabel.TLabel").grid(
                    row=card_row,
                    column=0,
                    sticky="nw",
                    padx=(0, 12),
                    pady=2,
                )
                value_label = ttk.Label(
                    card,
                    text=value,
                    justify=tk.LEFT,
                )
                value_label.grid(row=card_row, column=1, sticky="ew", pady=2)
                self._wrappable_labels.append(value_label)
                card_row += 1
            body = section.get("body") or ()
            if isinstance(body, str):
                body = [body]
            body_tone = str(section.get("tone") or "neutral")
            body_style = {
                "danger": "DangerText.TLabel",
                "severe": "SevereText.TLabel",
                "attention": "AttentionText.TLabel",
                "success": "SuccessText.TLabel",
                "info": "InfoText.TLabel",
            }.get(body_tone, "Muted.TLabel")
            for value in body:
                text = str(value or "").strip()
                if not text:
                    continue
                body_label = ttk.Label(
                    card,
                    text=f"• {text}",
                    style=body_style,
                    justify=tk.LEFT,
                )
                body_label.grid(row=card_row, column=0, columnspan=2, sticky="ew", pady=2)
                self._wrappable_labels.append(body_label)
                card_row += 1
            row += 1

        if normalized_events:
            card = ttk.Frame(self.summary, style="DetailCard.TFrame", padding=(13, 11))
            card.grid(row=row, column=0, sticky="ew", pady=(0, 4))
            card.columnconfigure(0, weight=1)
            events_header = ttk.Frame(card)
            events_header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
            ttk.Frame(events_header, style="NavLine.TFrame", width=3, height=16).pack(
                side=tk.LEFT,
                padx=(0, 7),
            )
            ttk.Label(events_header, text="最近事件", style="SectionTitle.TLabel").pack(side=tk.LEFT)
            for index, event in enumerate(normalized_events, start=1):
                event_label = ttk.Label(
                    card,
                    text=f"●  {event}",
                    style="Muted.TLabel",
                    justify=tk.LEFT,
                )
                event_label.grid(row=index, column=0, sticky="ew", pady=2)
                self._wrappable_labels.append(event_label)

        self.summary.update_idletasks()
        self._sync_wraplengths()
        self._sync_scroll_region()
        self.canvas.yview_moveto(0.0)

    def set_technical_text(self, text: str) -> None:
        self.technical_text.configure(state="normal")
        self.technical_text.delete("1.0", tk.END)
        self.technical_text.insert("1.0", str(text or ""))
        self.technical_text.configure(state="disabled")
