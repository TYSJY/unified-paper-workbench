from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from ..workspace import (
    WorkspaceDescriptor,
    WorkspaceError,
    list_registered_workspaces,
    load_workspace,
    register_workspace,
    remove_registered_workspace,
    suggested_workspace_directory,
)


CreateRequest = Callable[[Path, str, bool, "WorkspaceManagerWindow"], None]
SwitchRequest = Callable[[Path | None], None]


class WorkspaceManagerWindow(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        *,
        active: WorkspaceDescriptor,
        legacy_root: Path,
        on_create_request: CreateRequest,
        on_switch_request: SwitchRequest,
    ) -> None:
        super().__init__(master)
        self.title("论文工作区")
        self.geometry("1040x560")
        self.minsize(820, 440)
        self.transient(master)
        self.active = active
        self.legacy_root = legacy_root
        self.on_create_request = on_create_request
        self.on_switch_request = on_switch_request
        self._rows: dict[str, dict[str, object]] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self.active_var = tk.StringVar()
        ttk.Label(root, textvariable=self.active_var, font=("", 10, "bold")).grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 4),
        )
        ttk.Label(
            root,
            text=(
                "一个工作区保存一组论文、阶段状态、工作流和 SDK 会话。"
                "切换时 EXE 会自动以新工作区重新打开。"
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 10))

        columns = ("name", "path", "mode", "status")
        self.tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("name", text="工作区")
        self.tree.heading("path", text="目录")
        self.tree.heading("mode", text="类型")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=210, anchor=tk.W)
        self.tree.column("path", width=540, anchor=tk.W)
        self.tree.column("mode", width=110, anchor=tk.CENTER)
        self.tree.column("status", width=110, anchor=tk.CENTER)
        self.tree.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(root, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", lambda _event: self._switch_selected())

        actions = ttk.Frame(root)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="切换到选中工作区", command=self._switch_selected).pack(
            side=tk.LEFT,
            padx=(0, 6),
        )
        ttk.Button(actions, text="新建空白工作区", command=lambda: self._request_create(False)).pack(
            side=tk.LEFT,
            padx=3,
        )
        ttk.Button(actions, text="导入当前全部数据", command=lambda: self._request_create(True)).pack(
            side=tk.LEFT,
            padx=3,
        )
        ttk.Button(actions, text="添加已有工作区", command=self._add_existing).pack(
            side=tk.LEFT,
            padx=3,
        )
        ttk.Button(actions, text="打开目录", command=self._open_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="从列表移除", command=self._remove_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="关闭", command=self.destroy).pack(side=tk.RIGHT)

    def refresh(self) -> None:
        self.active_var.set(f"当前工作区：{self.active.name}　｜　{self.active.root}")
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows.clear()

        legacy = {
            "workspace_id": "legacy-current",
            "name": "旧版兼容工作区",
            "path": str(self.legacy_root),
            "mode": "legacy_bridge",
            "exists": True,
            "manifest_exists": False,
            "active": self.active.is_legacy_bridge,
        }
        rows = [legacy, *list_registered_workspaces()]
        for index, row in enumerate(rows):
            workspace_id = str(row.get("workspace_id") or f"workspace-{index}")
            exists = bool(row.get("exists"))
            is_active = bool(row.get("active")) or workspace_id == self.active.workspace_id
            status = "当前使用" if is_active else ("可用" if exists else "目录不存在")
            mode = "旧版兼容" if str(row.get("mode") or "") == "legacy_bridge" else "独立工作区"
            item_id = self.tree.insert(
                "",
                tk.END,
                values=(
                    str(row.get("name") or ""),
                    str(row.get("path") or ""),
                    mode,
                    status,
                ),
            )
            self._rows[item_id] = dict(row)
            if is_active:
                self.tree.selection_set(item_id)
                self.tree.focus(item_id)

    def _selected(self) -> dict[str, object] | None:
        selected = self.tree.selection()
        return self._rows.get(selected[0]) if selected else None

    def _switch_selected(self) -> None:
        row = self._selected()
        if not row:
            messagebox.showinfo("论文工作区", "请先选择一个工作区。", parent=self)
            return
        workspace_id = str(row.get("workspace_id") or "")
        if workspace_id == self.active.workspace_id:
            messagebox.showinfo("论文工作区", "当前已经在使用这个工作区。", parent=self)
            return
        if workspace_id == "legacy-current":
            self.on_switch_request(None)
            return
        path = Path(str(row.get("path") or ""))
        if not path.is_dir():
            messagebox.showerror("论文工作区", f"工作区目录不存在：\n{path}", parent=self)
            return
        self.on_switch_request(path)

    def _request_create(self, import_current: bool) -> None:
        parent = filedialog.askdirectory(
            parent=self,
            title="选择新工作区的上级目录",
            mustexist=True,
        )
        if not parent:
            return
        default_name = f"{self.active.name} - 独立版" if import_current else "新论文项目"
        name = simpledialog.askstring(
            "工作区名称",
            "请输入工作区显示名称：",
            initialvalue=default_name,
            parent=self,
        )
        if not name or not name.strip():
            return
        suggested = suggested_workspace_directory(parent, name)
        folder_name = simpledialog.askstring(
            "工作区目录",
            "请输入新建文件夹名称（建议使用英文和数字）：",
            initialvalue=suggested.name,
            parent=self,
        )
        if not folder_name or not folder_name.strip():
            return
        target = Path(parent) / folder_name.strip()
        if import_current:
            confirmed = messagebox.askyesno(
                "导入当前全部数据",
                (
                    f"将把当前工作区的论文、状态和必要运行资料复制到：\n{target}\n\n"
                    "旧目录不会移动或删除；构建缓存、异常 Git 对象、Chrome Profile 和旧迁移包不会复制。"
                    "\n\n是否继续？"
                ),
                parent=self,
            )
            if not confirmed:
                return
        self.on_create_request(target, name.strip(), import_current, self)

    def _add_existing(self) -> None:
        selected = filedialog.askdirectory(
            parent=self,
            title="选择包含 workspace.json 的论文工作区",
            mustexist=True,
        )
        if not selected:
            return
        try:
            descriptor = load_workspace(selected)
            register_workspace(descriptor)
        except WorkspaceError as exc:
            messagebox.showerror("添加工作区失败", str(exc), parent=self)
            return
        self.refresh()

    def _open_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        path = Path(str(row.get("path") or ""))
        if not path.exists():
            messagebox.showerror("论文工作区", f"目录不存在：\n{path}", parent=self)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开目录失败", str(exc), parent=self)

    def _remove_selected(self) -> None:
        row = self._selected()
        if not row:
            return
        workspace_id = str(row.get("workspace_id") or "")
        if workspace_id == "legacy-current":
            messagebox.showinfo("论文工作区", "旧版兼容入口不能从列表移除。", parent=self)
            return
        if workspace_id == self.active.workspace_id:
            messagebox.showinfo("论文工作区", "不能移除当前正在使用的工作区。", parent=self)
            return
        if not messagebox.askyesno(
            "从列表移除",
            "只从 EXE 的工作区列表移除，不会删除目录或任何论文文件。是否继续？",
            parent=self,
        ):
            return
        remove_registered_workspace(workspace_id)
        self.refresh()


__all__ = ["WorkspaceManagerWindow"]
