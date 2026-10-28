from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from ..config import (
    FIGURE_MAX_PARALLEL_WORKERS,
    FULL_PIPELINE_MAX_PAPERS,
    JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS,
    WRITING_MAX_PARALLEL_WORKERS,
)
from ..writing_gate_policy import AUTO_TRUST_MODE
from .theme import THEME


_PHASE_LABELS = {
    "writing": "写论文",
    "figures": "图表与插图",
    "journal_author": "选刊与作者",
    "smart_submission": "智能投稿",
    "completed": "已提交",
}

_STATUS_LABELS = {
    "awaiting_writing_identity": "正在建立论文编号",
    "waiting_projection": "正在同步状态",
    "needs_attention": "需要处理",
    "complete": "本阶段完成",
    "completed": "已完成",
    "queued": "已排队",
    "running": "运行中",
    "monitoring": "已投稿并监控",
    "submitted": "已投稿",
    "accepted": "已接收",
}


class FullPipelineWindow(tk.Toplevel):
    """Workspace-scoped control center for one-click end-to-end batches."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        orchestrator: Any,
        runtime: Any,
        run_background: Callable[[str, Callable[[], Any], Callable[[Any], None]], None],
        on_changed: Callable[[], None],
        on_closed: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.orchestrator = orchestrator
        self.runtime = runtime
        self.run_background = run_background
        self.on_changed = on_changed
        self.on_closed = on_closed
        self._poll_after_id: str | None = None
        self._last_revision = -1
        self.title("一键全流程")
        self.geometry("1120x680")
        self.minsize(820, 540)
        self.configure(background=THEME.canvas)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._refresh_gate_policy()
        self._refresh_status(force=True)
        self._schedule_poll()

    def _build(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=(14, 12))
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        heading = ttk.Frame(root, style="Header.TFrame", padding=(12, 10))
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="一键跑完全程", style="AppTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        subtitle = ttk.Label(
            heading,
            text="新建论文后由程序依次交接：写作 → 图表 → 选刊作者 → 智能投稿 → 自动最终提交。",
            style="AppSubtitle.TLabel",
            justify=tk.LEFT,
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(3, 0))
        heading.bind(
            "<Configure>",
            lambda event: subtitle.configure(wraplength=max(420, int(event.width) - 28)),
            add="+",
        )

        controls = ttk.LabelFrame(root, text="启动设置", padding=(12, 9))
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        controls.columnconfigure(4, weight=1)
        ttk.Label(controls, text="本次新建并发论文数").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.parallel_var = tk.IntVar(value=1)
        self.parallel_spinbox = ttk.Spinbox(
            controls,
            from_=1,
            to=FULL_PIPELINE_MAX_PAPERS,
            textvariable=self.parallel_var,
            width=7,
            state="readonly",
        )
        self.parallel_spinbox.grid(row=0, column=1, sticky="w", pady=3)
        self.limit_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.limit_var, style="Muted.TLabel").grid(
            row=0, column=2, columnspan=3, sticky="w", padx=(12, 8), pady=3
        )
        self.parallel_var.trace_add("write", lambda *_args: self._update_limit_text())
        self._update_limit_text()

        self.gate_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.gate_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(7, 3)
        )
        ttk.Button(
            controls,
            text="设置 Step 5 自动信任…",
            command=self._enable_auto_trust,
            style="Quiet.TButton",
        ).grid(row=1, column=4, sticky="e", pady=(7, 3))

        actions = ttk.Frame(root, style="CommandBar.TFrame", padding=(8, 7))
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        action_buttons = ttk.Frame(actions, style="CommandBar.TFrame")
        action_buttons.grid(row=0, column=0, sticky="w")
        ttk.Button(
            action_buttons,
            text="开始跑完全程",
            command=self._start,
            style="Accent.TButton",
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            action_buttons,
            text="暂停后续自动交接",
            command=self._pause,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(action_buttons, text="继续自动交接", command=self._resume).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(
            action_buttons,
            text="刷新状态",
            command=lambda: self._refresh_status(force=True),
            style="Quiet.TButton",
        ).pack(side=tk.LEFT, padx=3)
        action_hint = ttk.Label(
            actions,
            text="暂停只停止阶段交接，不会强制中断正在执行的 Step。",
            style="Muted.TLabel",
        )
        action_hint.grid(row=0, column=1, sticky="e", padx=4)
        actions.columnconfigure(1, weight=1)
        action_layout = {"compact": False}

        def reflow_actions(event: tk.Event) -> None:
            compact = int(getattr(event, "width", actions.winfo_width()) or 0) < 950
            if compact == action_layout["compact"]:
                return
            action_layout["compact"] = compact
            if compact:
                action_hint.grid_configure(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    padx=4,
                    pady=(7, 0),
                )
            else:
                action_hint.grid_configure(
                    row=0,
                    column=1,
                    columnspan=1,
                    sticky="e",
                    padx=4,
                    pady=(0, 0),
                )

        actions.bind("<Configure>", reflow_actions, add="+")

        body = ttk.Frame(root)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.summary_var = tk.StringVar(value="正在读取全流程状态…")
        ttk.Label(body, textvariable=self.summary_var).grid(
            row=0, column=0, sticky="ew", pady=(0, 6)
        )

        table = ttk.Frame(body)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("paper", "phase", "status", "progress", "detail")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        headings = {
            "paper": "论文 / 预留编号",
            "phase": "当前阶段",
            "status": "实时状态",
            "progress": "总进度",
            "detail": "当前动作 / 需要处理",
        }
        widths = {"paper": 270, "phase": 110, "status": 150, "progress": 80, "detail": 430}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=70 if column != "detail" else 180,
                stretch=column in {"paper", "detail"},
                anchor=tk.W if column != "progress" else tk.CENTER,
            )
        self.tree.tag_configure("running", background=THEME.accent_muted)
        self.tree.tag_configure("attention", background=THEME.danger_muted)
        self.tree.tag_configure("done", background=THEME.success_muted)
        self.tree.tag_configure("paused", background=THEME.attention_muted)
        vertical = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        self.error_var = tk.StringVar()
        ttk.Label(
            body,
            textvariable=self.error_var,
            foreground=THEME.danger,
            wraplength=1000,
            justify=tk.LEFT,
        ).grid(row=2, column=0, sticky="ew", pady=(7, 0))

    def _update_limit_text(self) -> None:
        try:
            parallel = max(1, min(int(self.parallel_var.get()), FULL_PIPELINE_MAX_PAPERS))
        except (TypeError, ValueError, tk.TclError):
            parallel = 1
        self.limit_var.set(
            f"写作 {min(parallel, WRITING_MAX_PARALLEL_WORKERS)} ｜ "
            f"画图 {min(parallel, FIGURE_MAX_PARALLEL_WORKERS)} ｜ "
            f"选刊作者 {min(parallel, JOURNAL_AUTHOR_MAX_PARALLEL_WORKERS)} ｜ "
            f"智能投稿 {parallel}"
        )

    def _refresh_gate_policy(self) -> dict[str, Any]:
        try:
            policy = self.orchestrator.writing_gate_policy()
        except Exception as exc:
            self.gate_var.set(f"Step 5 策略读取失败：{exc}")
            return {}
        enabled = str(policy.get("mode") or "") == AUTO_TRUST_MODE
        self.gate_var.set(
            "Step 5：自动信任并继续（符合一键全流程要求）"
            if enabled
            else "Step 5：严格确认；开始前需要管理密码切换为自动信任"
        )
        return policy

    def _ask_auto_trust_credentials(self, policy: dict[str, Any]) -> tuple[str, str] | None:
        password = simpledialog.askstring(
            "Step 5 自动信任",
            (
                "请输入现有管理密码："
                if policy.get("password_configured")
                else "首次启用需要设置管理密码（4-128 个字符）："
            ),
            show="*",
            parent=self,
        )
        if password is None:
            return None
        confirmation = ""
        if not policy.get("password_configured"):
            value = simpledialog.askstring(
                "确认管理密码",
                "请再次输入管理密码：",
                show="*",
                parent=self,
            )
            if value is None:
                return None
            confirmation = value
        return password, confirmation

    def _enable_auto_trust(self) -> None:
        policy = self._refresh_gate_policy()
        if str(policy.get("mode") or "") == AUTO_TRUST_MODE:
            messagebox.showinfo("Step 5 自动信任", "当前工作区已经启用。", parent=self)
            return
        credentials = self._ask_auto_trust_credentials(policy)
        if credentials is None:
            return
        password, confirmation = credentials

        def work() -> dict[str, Any]:
            return self.orchestrator.set_writing_gate_policy(
                AUTO_TRUST_MODE,
                password=password,
                password_confirmation=confirmation,
            )

        def completed(_result: Any) -> None:
            self._refresh_gate_policy()
            self.on_changed()
            messagebox.showinfo(
                "Step 5 自动信任",
                "当前工作区已启用；Step 5 完成后会直接进入 Step 6。",
                parent=self,
            )

        self.run_background("启用 Step 5 自动信任", work, completed)

    def _start(self) -> None:
        try:
            parallel = int(self.parallel_var.get())
        except (TypeError, ValueError, tk.TclError):
            messagebox.showerror("一键全流程", "并发数量无效。", parent=self)
            return
        if not 1 <= parallel <= FULL_PIPELINE_MAX_PAPERS:
            messagebox.showerror(
                "一键全流程",
                f"并发数量必须是 1-{FULL_PIPELINE_MAX_PAPERS}。",
                parent=self,
            )
            return
        policy = self._refresh_gate_policy()
        credentials: tuple[str, str] | None = None
        if str(policy.get("mode") or "") != AUTO_TRUST_MODE:
            credentials = self._ask_auto_trust_credentials(policy)
            if credentials is None:
                return
        if not messagebox.askyesno(
            "开始一键全流程",
            f"确定从 0 新建 {parallel} 篇论文并自动运行到最终投稿吗？\n\n"
            "程序会使用当前工作区的写作、画图、选刊作者提示词和逐 Step 模型设置；"
            "不会改写内部提示词。Step 5 使用自动信任模式，智能投稿会自动完成最终提交。",
            parent=self,
        ):
            return

        def work() -> dict[str, Any]:
            if credentials is not None:
                password, confirmation = credentials
                self.orchestrator.set_writing_gate_policy(
                    AUTO_TRUST_MODE,
                    password=password,
                    password_confirmation=confirmation,
                )
            return self.runtime.start_new(parallel)

        def completed(result: dict[str, Any]) -> None:
            self._refresh_gate_policy()
            self._render_status(result)
            self.on_changed()
            messagebox.showinfo(
                "一键全流程已启动",
                f"已新建 {parallel} 篇论文。后续阶段会在各自完成后自动交接；"
                "关闭 EXE 后重新打开也会从账本继续。",
                parent=self,
            )

        self.run_background("启动一键全流程", work, completed)

    def _pause(self) -> None:
        self.run_background("暂停全流程自动交接", self.runtime.pause, self._operation_completed)

    def _resume(self) -> None:
        self.run_background("继续全流程自动交接", self.runtime.resume, self._operation_completed)

    def _operation_completed(self, result: dict[str, Any]) -> None:
        self._render_status(self.runtime.status() if result.get("ok") else result)
        self.on_changed()

    def _schedule_poll(self) -> None:
        if self.winfo_exists():
            self._poll_after_id = self.after(1_500, self._poll)

    def _poll(self) -> None:
        self._poll_after_id = None
        self._refresh_status()
        self._schedule_poll()

    def _refresh_status(self, *, force: bool = False) -> None:
        try:
            status = self.runtime.status()
        except Exception as exc:
            self.summary_var.set("一键全流程状态暂时无法读取")
            self.error_var.set(str(exc))
            return
        revision = int(status.get("revision") or 0)
        if force or revision != self._last_revision:
            self._render_status(status)
            self._last_revision = revision

    def _render_status(self, status: dict[str, Any]) -> None:
        self.summary_var.set(str(status.get("summary") or "一键全流程：未启动"))
        self.error_var.set(str(status.get("last_error") or ""))
        rows = [item for item in status.get("papers", []) if isinstance(item, dict)]
        desired: list[str] = []
        for index, item in enumerate(rows, start=1):
            iid = f"slot-{int(item.get('slot') or index)}"
            desired.append(iid)
            paper_id = str(item.get("paper_id") or "").strip()
            reserved = str(item.get("reserved_pipeline_prefix") or "").strip()
            paper_label = paper_id or (f"{reserved}_…（编号生成中）" if reserved else f"任务 {index}")
            phase = str(item.get("phase") or "writing")
            raw_status = str(item.get("status") or "")
            values = (
                paper_label,
                _PHASE_LABELS.get(phase, phase),
                _STATUS_LABELS.get(raw_status, raw_status or "等待"),
                f"{int(item.get('progress_percent') or 0)}%",
                str(item.get("detail") or item.get("last_error") or ""),
            )
            tag = (
                "done"
                if phase == "completed"
                else "attention"
                if raw_status == "needs_attention"
                else "paused"
                if str(status.get("status") or "") == "paused"
                else "running"
            )
            if self.tree.exists(iid):
                self.tree.item(iid, values=values, tags=(tag,))
                if self.tree.index(iid) != index - 1:
                    self.tree.move(iid, "", index - 1)
            else:
                self.tree.insert("", index - 1, iid=iid, values=values, tags=(tag,))
        for iid in self.tree.get_children(""):
            if str(iid) not in desired:
                self.tree.delete(iid)

    def _close(self) -> None:
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        self.on_closed()
        self.destroy()
