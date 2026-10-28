from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from . import __version__
from .api import V2Application, V2HTTPServer
from .config import (
    DATABASE_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LEGACY_UNIFIED_STATE_PATH,
    REPO_ROOT,
    ensure_runtime,
    read_api_token,
)
from .kernel import WorkflowKernel
from .store import EventStore


class LocalApiController:
    """Own the loopback HTTP server lifecycle without coupling it to Tk."""

    def __init__(self, kernel: WorkflowKernel) -> None:
        self.kernel = kernel
        self.app: V2Application | None = None
        self.server: V2HTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> tuple[str, int]:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("V2 local API may bind only to loopback")
        if self.running and self.server:
            address = self.server.server_address
            return str(address[0]), int(address[1])

        app = V2Application(self.kernel, read_api_token(), run_worker=False)
        try:
            server = V2HTTPServer((host, int(port)), app)
        except Exception:
            app.close()
            raise
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="v2-local-api",
            daemon=True,
        )
        self.app = app
        self.server = server
        self.thread = thread
        thread.start()
        address = server.server_address
        return str(address[0]), int(address[1])

    def stop(self) -> None:
        server, app, thread = self.server, self.app, self.thread
        self.server = None
        self.app = None
        self.thread = None
        if server:
            server.shutdown()
            server.server_close()
        if app:
            app.close()
        if thread and thread.is_alive():
            thread.join(timeout=5)


class V2ControlPanel:
    REFRESH_MS = 2_000

    def __init__(self, root: tk.Tk, database: Path) -> None:
        self.root = root
        self.database = Path(database)
        self.kernel = WorkflowKernel(EventStore(self.database))
        self.api = LocalApiController(self.kernel)
        self._refresh_job: str | None = None

        self.summary_var = tk.StringVar(value="正在读取内部实验内核状态…")
        self.api_var = tk.StringVar(value="本地 API：未启动")
        self.database_var = tk.StringVar(value=f"独立数据库：{self.database}")
        self._configure_window()
        self._build_widgets()
        self.refresh()

    def _configure_window(self) -> None:
        self.root.title(f"内部工作流内核实验台（非产品入口）  ·  {__version__}")
        self.root.geometry("1120x720")
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#53606f")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        ttk.Label(heading, text="内部内核实验台", style="Title.TLabel").pack(side="left")
        ttk.Label(
            heading,
            text="非产品 V2 · 模块化单体 · 本地 API · 多执行器",
            style="Hint.TLabel",
        ).pack(side="left", padx=(16, 0), pady=(8, 0))

        ttk.Label(outer, textvariable=self.summary_var, style="Status.TLabel").pack(fill="x", pady=(14, 2))
        ttk.Label(outer, textvariable=self.database_var, style="Hint.TLabel").pack(fill="x")
        ttk.Label(outer, textvariable=self.api_var, style="Hint.TLabel").pack(fill="x", pady=(2, 10))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 12))
        ttk.Button(actions, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(actions, text="启动本地 API", command=self.start_api).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="停止本地 API", command=self.stop_api).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="执行一个已排队任务", command=self.dispatch_once).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="只读导入 V1 状态", command=self.import_legacy).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="打开架构说明", command=self.open_architecture_doc).pack(side="right")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        task_tab = ttk.Frame(notebook, padding=8)
        executor_tab = ttk.Frame(notebook, padding=8)
        event_tab = ttk.Frame(notebook, padding=8)
        notebook.add(task_tab, text="最近任务")
        notebook.add(executor_tab, text="执行器")
        notebook.add(event_tab, text="事件记录")

        self.tasks_tree = self._tree(
            task_tab,
            ("task_id", "paper_id", "stage", "action", "executor", "status", "updated_at"),
            {
                "task_id": ("任务 ID", 190),
                "paper_id": ("论文", 120),
                "stage": ("模块", 100),
                "action": ("动作", 180),
                "executor": ("执行器", 110),
                "status": ("状态", 120),
                "updated_at": ("更新时间", 175),
            },
        )
        self.executors_tree = self._tree(
            executor_tab,
            ("name", "available", "kind", "side_effecting", "detail"),
            {
                "name": ("执行器", 150),
                "available": ("可用", 80),
                "kind": ("类型", 140),
                "side_effecting": ("有外部副作用", 120),
                "detail": ("说明", 520),
            },
        )
        self.events_tree = self._tree(
            event_tab,
            ("event_id", "event_type", "task_id", "created_at", "payload"),
            {
                "event_id": ("ID", 70),
                "event_type": ("事件", 180),
                "task_id": ("任务 ID", 190),
                "created_at": ("时间", 175),
                "payload": ("摘要", 420),
            },
        )

        ttk.Label(
            outer,
            text="安全默认：打开界面不会自动运行任务；API 仅监听 127.0.0.1，且需要本地 Bearer Token。",
            style="Hint.TLabel",
        ).pack(fill="x", pady=(10, 0))

    @staticmethod
    def _tree(
        parent: ttk.Frame,
        columns: tuple[str, ...],
        headings: dict[str, tuple[str, int]],
    ) -> ttk.Treeview:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for column in columns:
            title, width = headings[column]
            tree.heading(column, text=title)
            tree.column(column, width=width, minwidth=60, stretch=column in {"detail", "payload"})
        return tree

    def refresh(self) -> None:
        try:
            status = self.kernel.status()
            runtime = status["runtime"]
            counts = runtime["task_counts"]
            total = sum(int(value) for value in counts.values())
            count_text = "、".join(f"{key}={value}" for key, value in sorted(counts.items())) or "暂无任务"
            self.summary_var.set(
                f"实验内核 {status['version']}  |  任务 {total}（{count_text}）  |  "
                f"事件 {runtime['event_count']}  |  V1 快照 {runtime['legacy_snapshot_count']}"
            )
            self._replace_tasks(self.kernel.store.list_tasks(limit=100))
            self._replace_executors(status["executors"])
            self._replace_events(self.kernel.store.list_events(limit=200))
        except Exception as exc:
            self.summary_var.set(f"刷新失败：{type(exc).__name__}: {exc}")
        finally:
            if self._refresh_job:
                self.root.after_cancel(self._refresh_job)
            self._refresh_job = self.root.after(self.REFRESH_MS, self.refresh)

    def _replace_tasks(self, records: list[dict[str, Any]]) -> None:
        self.tasks_tree.delete(*self.tasks_tree.get_children())
        for item in records:
            self.tasks_tree.insert(
                "",
                "end",
                values=tuple(item.get(key, "") for key in self.tasks_tree["columns"]),
            )

    def _replace_executors(self, records: list[dict[str, Any]]) -> None:
        self.executors_tree.delete(*self.executors_tree.get_children())
        for item in records:
            values = (
                item.get("name", ""),
                "是" if item.get("available") else "否",
                item.get("kind", ""),
                "是" if item.get("side_effecting") else "否",
                item.get("detail", ""),
            )
            self.executors_tree.insert("", "end", values=values)

    def _replace_events(self, records: list[dict[str, Any]]) -> None:
        self.events_tree.delete(*self.events_tree.get_children())
        for item in reversed(records):
            payload = json.dumps(item.get("payload", {}), ensure_ascii=False, separators=(",", ":"))
            if len(payload) > 180:
                payload = payload[:177] + "…"
            self.events_tree.insert(
                "",
                "end",
                values=(
                    item.get("event_id", ""),
                    item.get("event_type", ""),
                    item.get("task_id", ""),
                    item.get("created_at", ""),
                    payload,
                ),
            )

    def start_api(self) -> None:
        try:
            host, port = self.api.start()
            self.api_var.set(f"本地 API：已启动  http://{host}:{port}  （worker 未自动运行）")
        except Exception as exc:
            self.api_var.set(f"本地 API：启动失败，{type(exc).__name__}: {exc}")
            messagebox.showerror("无法启动本地 API", str(exc), parent=self.root)

    def stop_api(self) -> None:
        try:
            self.api.stop()
            self.api_var.set("本地 API：未启动")
        except Exception as exc:
            self.api_var.set(f"本地 API：停止失败，{type(exc).__name__}: {exc}")

    def dispatch_once(self) -> None:
        try:
            record = self.kernel.dispatch_once()
            if record is None:
                messagebox.showinfo("调度结果", "当前没有已排队任务。", parent=self.root)
            else:
                messagebox.showinfo(
                    "调度结果",
                    f"任务 {record['task_id']} 已经进入 {record['status']}。",
                    parent=self.root,
                )
            self.refresh()
        except Exception as exc:
            messagebox.showerror("调度失败", f"{type(exc).__name__}: {exc}", parent=self.root)

    def import_legacy(self) -> None:
        source = LEGACY_UNIFIED_STATE_PATH
        if not source.exists():
            messagebox.showwarning("V1 状态不存在", f"未找到：{source}", parent=self.root)
            return
        try:
            snapshot = self.kernel.import_legacy(source)
            messagebox.showinfo(
                "导入完成",
                f"已以只读方式记录 V1 快照 #{snapshot['snapshot_id']}\n"
                f"论文数：{snapshot['paper_count']}",
                parent=self.root,
            )
            self.refresh()
        except Exception as exc:
            messagebox.showerror("导入失败", f"{type(exc).__name__}: {exc}", parent=self.root)

    def open_architecture_doc(self) -> None:
        document = REPO_ROOT / "docs" / "V2_LOCAL_API_ARCHITECTURE.md"
        if not document.exists():
            messagebox.showwarning("文档不存在", str(document), parent=self.root)
            return
        try:
            os.startfile(document)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文档", str(exc), parent=self.root)

    def close(self) -> None:
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        self.api.stop()
        self.root.destroy()


def _detach_frozen_console() -> None:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            ctypes.windll.kernel32.FreeConsole()
        except (AttributeError, OSError):
            pass


def launch_ui(database: Path = DATABASE_PATH) -> None:
    ensure_runtime()
    _detach_frozen_console()
    root = tk.Tk()
    panel = V2ControlPanel(root, database)
    smoke_ms = int(os.environ.get("UNIFIED_V2_UI_SMOKE_MS", "0") or 0)
    if smoke_ms > 0:
        root.after(max(50, smoke_ms), panel.close)
    root.mainloop()
