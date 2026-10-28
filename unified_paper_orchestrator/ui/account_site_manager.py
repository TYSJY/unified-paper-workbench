from __future__ import annotations

import copy
import os
import re
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from ..account_site_store import (
    AccountSiteStore,
    DuplicateRecordError,
    RecordNotFoundError,
    RegistryValidationError,
)
from ..config import ACCOUNT_VAULT_PATH, LEGACY_CREDENTIAL_VAULT_PATH, STATE_DIR
from ..credential_vault import VaultError
from ..io_utils import JsonReadError
from .account_privacy import MASK, identity_tokens, mask_personal
from .presentation import (
    ACCOUNT_SCOPE_LABELS,
    STATUS_LABELS,
    filter_logical_groups,
    group_account_summaries,
    group_journal_records,
    group_submission_sites,
    responsive_window_size,
    status_label,
)


DEFAULT_CREDENTIAL_PATH = STATE_DIR / "smart_submission" / "credential_store.local.json"
DEFAULT_SITE_REGISTRY_PATH = STATE_DIR / "smart_submission" / "site_registry.json"
DEFAULT_ACCOUNT_VAULT_PATH = ACCOUNT_VAULT_PATH

_UI_LOCALE = str(os.environ.get("UNIFIED_PAPER_UI_LOCALE") or "zh_CN").strip().replace("-", "_")
_ENGLISH_UI = _UI_LOCALE.casefold().startswith("en")

_EN_UI_TEXT = {
    "全部分类": "All categories",
    "全部账号": "All accounts",
    "邮箱账号": "Email accounts",
    "投稿系统账号": "Submission accounts",
    "外部服务账号": "External services",
    "投稿站点": "Submission sites",
    "期刊记录": "Journal records",
    "账号与投稿站点": "Accounts and Submission Sites",
    "分类": "Categories",
    "搜索": "Search",
    "清除": "Clear",
    "名称": "Name",
    "账号或归属": "Account or owner",
    "新增": "Add",
    "编辑": "Edit",
    "更多操作": "More actions",
    "显示技术信息": "Show technical details",
    "请选择一条记录": "Select a record",
    "左侧列表仅做逻辑分组，不会改写底层数据。": (
        "The list groups related records for display only and does not rewrite stored data."
    ),
    "可先选择分类，再搜索网站、邮箱、账号或期刊名称。": (
        "Choose a category, then search by site, email, account, or journal."
    ),
    "账号分类": "Account category",
    "账号或邮箱": "Account or email",
    "网站/系统": "Website / system",
    "登录网址": "Login URL",
    "投稿网址": "Submission URL",
    "状态": "Status",
    "站点状态": "Site status",
    "记录来源": "Record sources",
    "关联期刊": "Related journals",
    "暂无": "None",
    "底层记录": "Source records",
    "来源": "Source",
    "保存方式": "Storage",
    "内部标识": "Internal ID",
    "内部标识（高级）": "Internal ID (advanced)",
    "账号": "Account",
    "邮箱": "Email",
    "用途": "Purpose",
    "登录状态": "Login status",
    "备注": "Notes",
    "投稿站点名称": "Submission site name",
    "稿件状态网址模板": "Manuscript-status URL template",
    "通知邮件发件人": "Notification email senders",
    "期刊名称": "Journal name",
    "关联论文": "Related papers",
    "新密码": "New password",
    "临时显示本次输入": "Temporarily show this entry",
    "取消": "Cancel",
    "保存": "Save",
    "关闭": "Close",
    "显示密码": "Show password",
    "复制密码": "Copy password",
    "临时查看密码": "Temporarily view password",
    "已复制": "Copied",
    "密码已复制；30 秒后会尝试清空剪贴板。": (
        "Password copied. The clipboard will be cleared after 30 seconds when possible."
    ),
    "留空表示保持原密码；填写后使用 Windows 当前用户加密保存。": (
        "Leave blank to keep the existing password. A new value is encrypted for the current Windows user."
    ),
    "此密码来自旧兼容明文记录。建议立即使用“迁移到 Windows 加密库”清理明文。": (
        "This password comes from a legacy plaintext record. Migrate it to the Windows vault promptly."
    ),
    "新账号使用 Windows 当前用户加密保存；旧稳定投稿加密库保持只读。"
    "三类来源分开保存、按账号逻辑合并展示；智能投稿由本地可信凭据代理"
    "直接填入可见浏览器，密码不会交给 AI 或写入调度状态。": (
        "New credentials are encrypted for the current Windows user. Legacy submission credentials remain "
        "read-only. Sources stay separate but are grouped by account; the trusted local credential broker "
        "fills the visible browser without exposing passwords to AI or scheduler state."
    ),
    "安全配置冲突：新账号库与旧稳定投稿库指向同一文件，账号新增和改写已停用。": (
        "Security configuration conflict: the new and legacy credential stores point to the same file. "
        "Adding or editing accounts is disabled."
    ),
    "编辑、查看密码或删除前，请先确认下面选中的具体来源。": (
        "Confirm the selected source record before editing, viewing its password, or deleting it."
    ),
    "内部标识只用于排错；日常使用无需查看或修改。": (
        "Internal IDs are only for diagnostics and normally do not need to be viewed or edited."
    ),
    "旧 Smart 明文记录可迁移到 Windows 当前用户加密库；迁移后运行态只保存加密引用。": (
        "Legacy Smart plaintext records can be migrated to the Windows vault; runtime state then stores only "
        "an encrypted reference."
    ),
    "已安全保存，可由本地可信凭据代理自动登录；密码不会暴露给 Codex SDK 会话。": (
        "Stored securely and available to the trusted local login broker. The password is not exposed to "
        "Codex SDK sessions."
    ),
    "旧稳定投稿加密库（只读）": "Legacy submission vault (read-only)",
    "旧稳定投稿库（只读）": "Legacy submission vault (read-only)",
    "新 Windows 加密账号库": "New Windows credential vault",
    "新 Windows 加密库": "New Windows vault",
    "智能投稿兼容记录": "Smart Submission compatibility record",
    "智能投稿兼容": "Smart Submission compatibility",
    "投稿站点记录": "Submission site record",
    "Windows 当前用户加密": "Encrypted for current Windows user",
    "Windows 当前用户加密（可自动登录）": "Windows user encryption (automatic login ready)",
    "已加密；旧明文待清理": "Encrypted; legacy plaintext pending cleanup",
    "已关联旧加密库": "Linked to legacy encrypted vault",
    "已关联旧库；明文待清理": "Linked to legacy vault; plaintext pending cleanup",
    "智能投稿旧明文（待迁移）": "Legacy Smart plaintext (migration required)",
    "加密引用需修复": "Encrypted reference needs repair",
    "未保存": "Not saved",
    "未知": "Unknown",
    "启用": "Active",
    "已验证": "Verified",
    "已登录": "Logged in",
    "会话可用": "Session ready",
    "未配置": "Not configured",
    "登录失败": "Login failed",
    "密码需核对": "Password needs review",
    "从旧账号库导入": "Imported from legacy account store",
    "从旧凭据库导入": "Imported from legacy credential store",
    "已配置（待登录验证）": "Configured (login not verified)",
    "已配置": "Configured",
    "可用": "Available",
    "适配计划中": "Adapter planned",
    "已发现": "Discovered",
    "待补充": "Needs details",
    "就绪": "Ready",
    "已阻塞": "Blocked",
    "来源状态不一致": "Source status conflict",
    "未分类账号": "Uncategorized account",
    "未命名记录": "Unnamed record",
    "未命名投稿站点": "Unnamed submission site",
    "未命名网站或系统": "Unnamed website or system",
    "未填写账号": "Account not provided",
    "已关联投稿系统": "Submission system linked",
    "未关联投稿系统": "No submission system linked",
    "密码和技术标识默认隐藏。": "Passwords and technical IDs are hidden by default.",
    "技术标识默认隐藏。": "Technical IDs are hidden by default.",
    "迁移选中旧明文到 Windows 加密库": "Migrate selected plaintext record to Windows vault",
    "迁移全部旧明文账号": "Migrate all legacy plaintext accounts",
    "删除所选底层记录…": "Delete selected source record…",
    "检查账号重复": "Check duplicate accounts",
    "删除所选站点记录…": "Delete selected site record…",
    "检查站点重复": "Check duplicate sites",
    "删除所选期刊记录…": "Delete selected journal record…",
    "检查期刊重复": "Check duplicate journals",
    "请先选择记录": "Select a record first",
    "账号与站点读取失败": "Could not read accounts and submission sites",
    "账号库安全保护": "Credential-store protection",
    "新增加密账号": "Add encrypted account",
    "账号保存失败": "Could not save account",
    "账号已安全保存": "Account stored securely",
    "账号读取失败": "Could not read account",
    "编辑新加密账号": "Edit encrypted account",
    "编辑智能投稿兼容记录": "Edit Smart Submission compatibility record",
    "确认删除账号": "Delete account?",
    "账号删除失败": "Could not delete account",
    "密码读取失败": "Could not read password",
    "未保存密码": "No saved password",
    "所选账号没有可读取的密码。": "The selected account has no readable saved password.",
    "无需迁移": "Migration not required",
    "迁移账号密码": "Migrate account password?",
    "密码迁移失败": "Password migration failed",
    "迁移完成": "Migration complete",
    "确认迁移全部旧明文账号": "Migrate all legacy plaintext accounts?",
    "批量迁移失败": "Bulk migration failed",
    "批量迁移完成": "Bulk migration complete",
    "新增投稿站点": "Add submission site",
    "编辑投稿站点": "Edit submission site",
    "投稿站点读取失败": "Could not read submission site",
    "投稿站点保存失败": "Could not save submission site",
    "确认删除投稿站点": "Delete submission site?",
    "投稿站点删除失败": "Could not delete submission site",
    "新增期刊": "Add journal",
    "编辑期刊": "Edit journal",
    "期刊记录读取失败": "Could not read journal record",
    "期刊记录保存失败": "Could not save journal record",
    "确认删除期刊": "Delete journal?",
    "期刊记录删除失败": "Could not delete journal record",
    "重复检查失败": "Duplicate check failed",
    "重复检查": "Duplicate check",
    "没有发现潜在重复记录。": "No potential duplicate records were found.",
    "发现潜在重复": "Potential duplicates found",
    "请先确认底层记录": "Confirm a source record first",
    "当前逻辑记录包含多条底层记录。请先打开“显示技术信息”，再选择要操作的具体记录。": (
        "This logical record contains multiple source records. Enable Show technical details, then select "
        "the exact source record to act on."
    ),
    "现有数据文件无法安全读取；为避免覆盖，操作已停止。请先检查文件或恢复备份。": (
        "The existing data file could not be read safely. The operation stopped to prevent overwriting it; "
        "check the file or restore a backup."
    ),
    "操作失败。为避免泄露敏感信息，详情未显示；源文件保持不变。": (
        "The operation failed. Sensitive details are hidden and the source file was left unchanged."
    ),
    "账号或站点库读取失败；没有执行写入。": (
        "Could not read the account or site store; no data was written."
    ),
}


def _ui_text(value: Any) -> str:
    text = str(value or "")
    if not _ENGLISH_UI:
        return text
    translated = _EN_UI_TEXT.get(text)
    if translated is not None:
        return translated
    text = re.sub(r"(\d+) 条底层记录", r"\1 source records", text)
    text = re.sub(r"(\d+) 本期刊", r"\1 journals", text)
    text = re.sub(r"（记录 (\d+)）", r" (record \1)", text)
    for chinese, english in (
        ("邮箱账号", "Email account"),
        ("投稿系统账号", "Submission account"),
        ("外部服务账号", "External-service account"),
        ("未分类账号", "Uncategorized account"),
    ):
        text = text.replace(chinese, english)
    return text


def _translate_widget_tree(widget: tk.Misc) -> None:
    if not _ENGLISH_UI:
        return
    try:
        current = widget.cget("text")
        if isinstance(current, str) and current:
            widget.configure(text=_ui_text(current))
    except (AttributeError, tk.TclError):
        pass
    for child in widget.winfo_children():
        _translate_widget_tree(child)


def _showinfo(title: str, message: str, **options: Any) -> Any:
    return messagebox.showinfo(_ui_text(title), _private_message(message, options), **options)


def _showwarning(title: str, message: str, **options: Any) -> Any:
    return messagebox.showwarning(_ui_text(title), _private_message(message, options), **options)


def _showerror(title: str, message: str, **options: Any) -> Any:
    return messagebox.showerror(_ui_text(title), _private_message(message, options), **options)


def _askyesno(title: str, message: str, **options: Any) -> bool:
    return bool(messagebox.askyesno(_ui_text(title), _private_message(message, options), **options))


def _private_message(message: str, options: dict[str, Any]) -> str:
    display = getattr(options.get("parent"), "_display", None)
    return _ui_text(display(message) if callable(display) else message)


SCOPE_LABEL_TO_VALUE = {
    _ui_text("全部分类"): "all",
    _ui_text("邮箱账号"): "email",
    _ui_text("投稿系统账号"): "journal_system",
    _ui_text("外部服务账号"): "external",
}
SCOPE_VALUE_TO_LABEL = {
    "email": _ui_text("邮箱账号"),
    "email_account": _ui_text("邮箱账号"),
    "journal": _ui_text("投稿系统账号"),
    "journal_system": _ui_text("投稿系统账号"),
    "submission": _ui_text("投稿系统账号"),
    "submission_system": _ui_text("投稿系统账号"),
    "external": _ui_text("外部服务账号"),
    "external_service": _ui_text("外部服务账号"),
}
STORAGE_LABELS = {key: _ui_text(label) for key, label in {
    "dpapi": "Windows 当前用户加密",
    "dpapi_legacy_read_only": "旧稳定投稿加密库（只读）",
    "dpapi_account_vault": "Windows 当前用户加密（可自动登录）",
    "dpapi_account_vault_plaintext_retained": "已加密；旧明文待清理",
    "dpapi_legacy_linked": "已关联旧加密库",
    "dpapi_legacy_linked_plaintext_retained": "已关联旧库；明文待清理",
    "plaintext_compatibility": "智能投稿旧明文（待迁移）",
    "broken_reference": "加密引用需修复",
    "none": "未保存",
}.items()}
CATEGORY_LABELS = {key: _ui_text(label) for key, label in {
    "accounts_all": "全部账号",
    "accounts_email": "邮箱账号",
    "accounts_journal_system": "投稿系统账号",
    "accounts_external": "外部服务账号",
    "sites": "投稿站点",
    "journals": "期刊记录",
}.items()}
UI_STATUS_LABELS = {key: _ui_text(label) for key, label in STATUS_LABELS.items()}


def _join_list(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value if str(item).strip())
    return str(value or "")


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _account_status_label(value: Any) -> str:
    return _ui_text(status_label(value))


def _status_label(value: Any) -> str:
    return _ui_text(status_label(value))


class _RecordDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        record: dict[str, Any],
        fields: list[dict[str, Any]],
        allow_password_input: bool = False,
        password_hint: str = "",
    ) -> None:
        super().__init__(parent)
        self.title(_ui_text(title))
        self.transient(parent.winfo_toplevel())
        self.resizable(True, True)
        self.result: dict[str, Any] | None = None
        self._original = copy.deepcopy(record)
        self._fields = fields
        self._vars: dict[str, tk.StringVar] = {}
        self._texts: dict[str, tk.Text] = {}
        self._privacy_entries: list[ttk.Entry] = []
        self._privacy_combo_states: dict[ttk.Combobox, str] = {}
        self._hidden_text_values: dict[str, str] = {}
        self.hide_sensitive_var = tk.BooleanVar(value=True)
        self._password_var = tk.StringVar(value="")
        self._show_password_var = tk.BooleanVar(value=False)
        self._first_input: tk.Widget | None = None

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        form_shell = ttk.Frame(outer)
        form_shell.grid(row=0, column=0, sticky="nsew")
        form_shell.columnconfigure(0, weight=1)
        form_shell.rowconfigure(0, weight=1)
        self._form_canvas = tk.Canvas(form_shell, borderwidth=0, highlightthickness=0)
        form_scroll = ttk.Scrollbar(form_shell, orient="vertical", command=self._form_canvas.yview)
        self._form_canvas.configure(yscrollcommand=form_scroll.set)
        self._form_canvas.grid(row=0, column=0, sticky="nsew")
        form_scroll.grid(row=0, column=1, sticky="ns")
        body = ttk.Frame(self._form_canvas, padding=(0, 0, 8, 0))
        body.columnconfigure(1, weight=1)
        form_window = self._form_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind(
            "<Configure>",
            lambda _event: self._form_canvas.configure(scrollregion=self._form_canvas.bbox("all")),
        )
        self._form_canvas.bind(
            "<Configure>",
            lambda event: self._form_canvas.itemconfigure(form_window, width=event.width),
        )
        row = 0
        ttk.Checkbutton(
            body, text="Hide personal information" if _ENGLISH_UI else "隐藏个人敏感信息",
            variable=self.hide_sensitive_var, command=self._toggle_privacy,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1
        for spec in fields:
            key = str(spec["key"])
            ttk.Label(body, text=_ui_text(spec["label"])).grid(
                row=row,
                column=0,
                sticky="nw",
                padx=(0, 10),
                pady=5,
            )
            kind = str(spec.get("kind") or "entry")
            value = _join_list(record.get(key)) if kind == "list" else str(record.get(key) or "")
            value = dict(spec.get("value_to_label") or {}).get(value, value)
            if kind in {"multiline", "list"}:
                widget = tk.Text(body, height=int(spec.get("height") or 4), wrap="word")
                widget.insert("1.0", value)
                widget.grid(row=row, column=1, sticky="nsew", pady=5)
                self._texts[key] = widget
                body.rowconfigure(row, weight=1)
            else:
                var = tk.StringVar(value=value)
                self._vars[key] = var
                if kind == "combo":
                    widget = ttk.Combobox(
                        body,
                        textvariable=var,
                        values=list(spec.get("values") or []),
                        state="readonly" if spec.get("readonly") else "normal",
                    )
                else:
                    widget = ttk.Entry(body, textvariable=var)
                    if spec.get("readonly"):
                        widget.configure(state="readonly")
                if kind != "combo" or key not in {"scope", "status"}:
                    self._privacy_entries.append(widget)
                    if isinstance(widget, ttk.Combobox):
                        self._privacy_combo_states[widget] = str(widget.cget("state"))
                widget.grid(row=row, column=1, sticky="ew", pady=5)
            if self._first_input is None and not spec.get("readonly"):
                self._first_input = widget
            row += 1

        self._toggle_privacy()
        if allow_password_input:
            ttk.Label(body, text=_ui_text("新密码")).grid(
                row=row,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=5,
            )
            password_entry = ttk.Entry(body, textvariable=self._password_var, show="*")
            password_entry.grid(row=row, column=1, sticky="ew", pady=5)
            row += 1
            if self._first_input is None:
                self._first_input = password_entry

            def toggle_password() -> None:
                password_entry.configure(show="" if self._show_password_var.get() else "*")

            ttk.Checkbutton(
                body,
                text=_ui_text("临时显示本次输入"),
                variable=self._show_password_var,
                command=toggle_password,
            ).grid(row=row, column=1, sticky="w")
            row += 1
            hint = password_hint or "留空表示保持原密码；填写后使用 Windows 当前用户加密保存。"
            ttk.Label(body, text=_ui_text(hint), foreground="#555", wraplength=480).grid(
                row=row,
                column=0,
                columnspan=2,
                sticky="ew",
                pady=(4, 8),
            )
            row += 1

        actions = ttk.Frame(outer)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text=_ui_text("取消"), command=self.destroy).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(actions, text=_ui_text("保存"), command=self._accept).pack(side=tk.RIGHT)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<MouseWheel>", self._scroll_form, add="+")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        self.update_idletasks()
        width, height, minimum_width, minimum_height = responsive_window_size(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            preferred=(max(620, self.winfo_reqwidth()), min(max(560, self.winfo_reqheight()), 780)),
            minimum=(560, 420),
            margins=(80, 140),
        )
        self.minsize(minimum_width, minimum_height)
        self.geometry(f"{width}x{height}")
        _translate_widget_tree(self)
        if self._first_input is not None:
            self.after_idle(self._first_input.focus_set)

    def _scroll_form(self, event: tk.Event[tk.Misc]) -> str | None:
        if not getattr(event, "delta", 0):
            return None
        self._form_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _accept(self) -> None:
        result = {key: value for key, value in self._original.items() if not str(key).startswith("_")}
        for spec in self._fields:
            key = str(spec["key"])
            kind = str(spec.get("kind") or "entry")
            if key in self._texts:
                value: Any = (self._hidden_text_values[key] if key in self._hidden_text_values
                              else self._texts[key].get("1.0", "end-1c")).strip()
            else:
                value = self._vars[key].get().strip()
            value_to_label = dict(spec.get("value_to_label") or {})
            original_value = str(self._original.get(key) or "")
            original_label = value_to_label.get(original_value, original_value)
            if original_value and value == original_label:
                value = original_value
            else:
                value = dict(spec.get("label_to_value") or {}).get(value, value)
            result[key] = _split_list(value) if kind == "list" else value
        result["_new_password"] = self._password_var.get()
        self._password_var.set("")
        self.result = result
        self.destroy()

    def _toggle_privacy(self) -> None:
        hidden = self.hide_sensitive_var.get()
        for widget in self._privacy_entries:
            widget.configure(show="*" if hidden else "")
            if widget in self._privacy_combo_states:
                widget.configure(state="disabled" if hidden else self._privacy_combo_states[widget])
        for key, widget in self._texts.items():
            if hidden and key not in self._hidden_text_values:
                self._hidden_text_values[key] = widget.get("1.0", "end-1c")
                widget.delete("1.0", "end")
                widget.insert("1.0", MASK)
                widget.configure(state="disabled")
            elif not hidden and key in self._hidden_text_values:
                widget.configure(state="normal")
                widget.delete("1.0", "end")
                widget.insert("1.0", self._hidden_text_values.pop(key))

    def show(self) -> dict[str, Any] | None:
        self.wait_window()
        return self.result


class _PasswordViewer(tk.Toplevel):
    def __init__(self, parent: tk.Misc, password: str, *, compatibility_plaintext: bool) -> None:
        super().__init__(parent)
        self.title(_ui_text("临时查看密码"))
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self._password_var = tk.StringVar(value=password)
        self._show_var = tk.BooleanVar(value=False)
        self._clipboard_host = parent._root()

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        entry = ttk.Entry(frame, textvariable=self._password_var, show="*", width=48, state="readonly")
        entry.grid(row=0, column=0, columnspan=3, sticky="ew")

        def toggle() -> None:
            entry.configure(show="" if self._show_var.get() else "*")

        ttk.Checkbutton(frame, text=_ui_text("显示密码"), variable=self._show_var, command=toggle).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        ttk.Button(frame, text=_ui_text("复制密码"), command=self._copy_password).grid(
            row=1,
            column=1,
            padx=8,
            pady=(8, 0),
        )
        ttk.Button(frame, text=_ui_text("关闭"), command=self.destroy).grid(
            row=1,
            column=2,
            pady=(8, 0),
        )
        if compatibility_plaintext:
            ttk.Label(
                frame,
                text=_ui_text(
                    "此密码来自旧兼容明文记录。建议立即使用“迁移到 Windows 加密库”清理明文。"
                ),
                foreground="#a45b00",
                wraplength=460,
            ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        _translate_widget_tree(self)

    def _copy_password(self) -> None:
        password = self._password_var.get()
        if not password:
            return
        self._clipboard_host.clipboard_clear()
        self._clipboard_host.clipboard_append(password)
        self._clipboard_host.update()
        self._clipboard_host.after(30000, lambda expected=password: self._clear_clipboard_if_matches(expected))
        _showinfo(
            _ui_text("已复制"),
            _ui_text("密码已复制；30 秒后会尝试清空剪贴板。"),
            parent=self,
        )

    def _clear_clipboard_if_matches(self, expected: str) -> None:
        try:
            if self._clipboard_host.clipboard_get() == expected:
                self._clipboard_host.clipboard_clear()
                self._clipboard_host.update()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        self._password_var.set("")
        super().destroy()


class AccountSiteManager(tk.Toplevel):
    """Shared account, submission-system, and journal registry window."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        credential_path: Path | str = DEFAULT_CREDENTIAL_PATH,
        site_registry_path: Path | str = DEFAULT_SITE_REGISTRY_PATH,
        vault_path: Path | str | None = DEFAULT_ACCOUNT_VAULT_PATH,
        legacy_vault_path: Path | str = LEGACY_CREDENTIAL_VAULT_PATH,
        backup_dir: Path | str | None = None,
        audit_path: Path | str | None = None,
        on_change: Callable[[], None] | None = None,
        store: AccountSiteStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(_ui_text("账号与投稿站点"))
        width, height, minimum_width, minimum_height = responsive_window_size(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            preferred=(1100, 680),
            minimum=(760, 500),
        )
        self.geometry(f"{width}x{height}")
        self.minsize(minimum_width, minimum_height)
        self.transient(parent.winfo_toplevel())
        self.store = store or AccountSiteStore(
            credential_path,
            site_registry_path,
            vault_path=vault_path,
            legacy_vault_path=legacy_vault_path,
            # Keep every writer on the credential store's canonical backup
            # root.  Passing the application's global backup directory here
            # made committed transactions created by Smart Submission appear
            # untrusted when this window reopened them.
            backup_dir=backup_dir,
            audit_path=audit_path,
        )
        self.on_change = on_change
        self._category_ids: dict[str, str] = {}
        self._group_items: dict[str, dict[str, Any]] = {}
        self._detail_record_ids: dict[str, str] = {}
        self._active_group: dict[str, Any] | None = None
        self._logical_groups: dict[str, list[dict[str, Any]]] = {}
        self._physical_counts = {"accounts": 0, "systems": 0, "journals": 0}

        self.search_var = tk.StringVar()
        self.category_var = tk.StringVar(value="accounts_all")
        self.show_technical_var = tk.BooleanVar(value=False)
        self.hide_sensitive_var = tk.BooleanVar(value=True)
        self._personal_tokens: set[str] = set()
        self.detail_title_var = tk.StringVar(value=_ui_text("请选择一条记录"))
        self.detail_subtitle_var = tk.StringVar(
            value=_ui_text("左侧列表仅做逻辑分组，不会改写底层数据。")
        )
        self.status_var = tk.StringVar(value="")

        self._build_ui()
        _translate_widget_tree(self)
        self.bind("<F5>", lambda _event: self.refresh_all(show_error=True))
        self.bind("<Control-f>", self._focus_search)
        self.bind("<Control-F>", self._focus_search)
        self.bind("<MouseWheel>", self._scroll_detail, add="+")
        self.after_idle(lambda: self.refresh_all(show_error=True))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)

        policy_text = (
            "安全配置冲突：新账号库与旧稳定投稿库指向同一文件，账号新增和改写已停用。"
            if self.store.vault_read_only
            else (
                "新账号使用 Windows 当前用户加密保存；旧稳定投稿加密库保持只读。"
                "三类来源分开保存、按账号逻辑合并展示；智能投稿由本地可信凭据代理"
                "直接填入可见浏览器，密码不会交给 AI 或写入调度状态。"
            )
        )
        policy_label = ttk.Label(outer, text=policy_text, foreground="#555", wraplength=1040)
        policy_label.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(0, 8),
        )
        policy_label.bind(
            "<Configure>",
            lambda event: policy_label.configure(wraplength=max(320, event.width)),
        )

        panes = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        ttk.Checkbutton(
            outer, text="Hide personal information" if _ENGLISH_UI else "隐藏个人敏感信息",
            variable=self.hide_sensitive_var, command=self._toggle_privacy,
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))
        panes.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(panes, padding=(0, 0, 8, 0), width=390)
        right = ttk.Frame(panes, padding=(10, 4, 4, 4))
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        self._build_master_pane(left)
        self._build_detail_pane(right)
        ttk.Label(outer, textvariable=self.status_var, anchor="w").grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def _build_master_pane(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(3, weight=1)
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, text="分类", font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.category_tree = ttk.Treeview(parent, show="tree", selectmode="browse", height=6)
        self.category_tree.grid(row=1, column=0, sticky="ew", pady=(5, 10))
        self.category_tree.bind("<<TreeviewSelect>>", self._on_category_selected)

        search_holder = ttk.Frame(parent)
        search_holder.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        search_holder.columnconfigure(1, weight=1)
        ttk.Label(search_holder, text="搜索").grid(row=0, column=0, padx=(0, 6))
        self.search_entry = ttk.Entry(search_holder, textvariable=self.search_var, show="*")
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.search_entry.bind("<KeyRelease>", lambda _event: self._refresh_master_list())
        ttk.Button(search_holder, text="清除", command=self._clear_search).grid(row=0, column=2, padx=(6, 0))

        holder = ttk.Frame(parent)
        holder.grid(row=3, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.master_tree = ttk.Treeview(
            holder,
            columns=("summary",),
            show="tree headings",
            selectmode="browse",
        )
        self.master_tree.heading("#0", text=_ui_text("名称"))
        self.master_tree.heading("summary", text=_ui_text("账号或归属"))
        self.master_tree.column("#0", width=190, minwidth=120, stretch=True)
        self.master_tree.column("summary", width=190, minwidth=110, stretch=True)
        vertical = ttk.Scrollbar(holder, orient="vertical", command=self.master_tree.yview)
        self.master_tree.configure(yscrollcommand=vertical.set)
        self.master_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        self.master_tree.bind("<<TreeviewSelect>>", self._on_group_selected)
        self.master_tree.bind("<Double-1>", lambda _event: self._edit_selected_record())

    def _build_detail_pane(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.detail_title_var, font=("TkDefaultFont", 14, "bold")).grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(
            header,
            textvariable=self.detail_subtitle_var,
            foreground="#555",
            wraplength=540,
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 10))
        self.add_button = ttk.Button(actions, text="新增", command=self._new_for_current_category)
        self.add_button.pack(side=tk.LEFT)
        self.edit_button = ttk.Button(actions, text="编辑", command=self._edit_selected_record)
        self.edit_button.pack(side=tk.LEFT, padx=(6, 0))
        self.more_button = ttk.Menubutton(actions, text="更多操作")
        self.more_menu = tk.Menu(self.more_button, tearoff=False)
        self.more_button.configure(menu=self.more_menu)
        self.more_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Checkbutton(
            actions,
            text="显示技术信息",
            variable=self.show_technical_var,
            command=self._render_active_group,
        ).pack(side=tk.RIGHT)

        detail_holder = ttk.Frame(parent)
        detail_holder.grid(row=2, column=0, sticky="nsew")
        detail_holder.rowconfigure(0, weight=1)
        detail_holder.columnconfigure(0, weight=1)
        self.detail_canvas = tk.Canvas(detail_holder, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(detail_holder, orient="vertical", command=self.detail_canvas.yview)
        self.detail_canvas.configure(yscrollcommand=scrollbar.set)
        self.detail_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.detail_frame = ttk.Frame(self.detail_canvas, padding=(2, 0, 8, 8))
        self._detail_window = self.detail_canvas.create_window((0, 0), window=self.detail_frame, anchor="nw")
        self.detail_frame.bind(
            "<Configure>",
            lambda _event: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all")),
        )
        self.detail_canvas.bind(
            "<Configure>",
            lambda event: self.detail_canvas.itemconfigure(self._detail_window, width=event.width),
        )
        self._render_empty_detail()

    def _focus_search(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        self.search_entry.focus_set()
        self.search_entry.selection_range(0, tk.END)
        return "break"

    def _scroll_detail(self, event: tk.Event[tk.Misc]) -> str | None:
        if not getattr(event, "delta", 0):
            return None
        x_root = int(getattr(event, "x_root", -1))
        y_root = int(getattr(event, "y_root", -1))
        left = self.detail_canvas.winfo_rootx()
        top = self.detail_canvas.winfo_rooty()
        if not (
            left <= x_root <= left + self.detail_canvas.winfo_width()
            and top <= y_root <= top + self.detail_canvas.winfo_height()
        ):
            return None
        self.detail_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _show_error(self, title: str, error: BaseException) -> None:
        if isinstance(error, JsonReadError):
            message = "现有数据文件无法安全读取；为避免覆盖，操作已停止。请先检查文件或恢复备份。"
        elif isinstance(error, (RegistryValidationError, DuplicateRecordError, RecordNotFoundError, VaultError)):
            message = str(error)
        else:
            message = "操作失败。为避免泄露敏感信息，详情未显示；源文件保持不变。"
        _showerror(title, message, parent=self)

    def _notify_changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    def refresh_all(self, *, show_error: bool = False) -> None:
        try:
            accounts = self.store.list_account_summaries()
            systems = self.store.list_systems()
            journals = self.store.list_journals()
        except Exception as exc:
            self.status_var.set("账号或站点库读取失败；没有执行写入。")
            if show_error:
                self._show_error("账号与站点读取失败", exc)
            return
        self._personal_tokens = identity_tokens([accounts, systems, journals])
        account_groups = group_account_summaries(accounts)
        system_labels = {
            str(system.get("system_id") or ""): str(system.get("label") or "未命名投稿站点")
            for system in systems
        }
        journal_view_rows = [
            {**journal, "system_label": system_labels.get(str(journal.get("system_id") or ""), "")}
            for journal in journals
        ]
        self._logical_groups = {
            "accounts_all": account_groups,
            "accounts_email": [group for group in account_groups if group.get("scope") == "email"],
            "accounts_journal_system": [
                group for group in account_groups if group.get("scope") == "journal_system"
            ],
            "accounts_external": [group for group in account_groups if group.get("scope") == "external"],
            "sites": group_submission_sites(systems, journals),
            "journals": group_journal_records(journal_view_rows),
        }
        self._physical_counts = {
            "accounts": len(accounts),
            "systems": len(systems),
            "journals": len(journals),
        }
        previous_key = str((self._active_group or {}).get("group_key") or "")
        self._refresh_category_tree()
        self._refresh_master_list(preferred_group_key=previous_key)

    def refresh_accounts(self, *, show_error: bool = False) -> None:
        self.refresh_all(show_error=show_error)

    def refresh_systems(self, *, show_error: bool = False) -> None:
        self.refresh_all(show_error=show_error)

    def refresh_journals(self, *, show_error: bool = False) -> None:
        self.refresh_all(show_error=show_error)

    def _refresh_category_tree(self) -> None:
        selected_category = self.category_var.get() or "accounts_all"
        self.category_tree.delete(*self.category_tree.get_children())
        self._category_ids.clear()
        for index, (category, label) in enumerate(CATEGORY_LABELS.items()):
            count = len(self._logical_groups.get(category, []))
            item_id = f"category-{index}"
            self._category_ids[item_id] = category
            self.category_tree.insert("", "end", iid=item_id, text=f"{label}  ({count})")
            if category == selected_category:
                self.category_tree.selection_set(item_id)
                self.category_tree.focus(item_id)
        if selected_category not in self._category_ids.values():
            first = next(iter(self._category_ids), "")
            if first:
                self.category_tree.selection_set(first)
                self.category_var.set(self._category_ids[first])

    def _on_category_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selected = self.category_tree.selection()
        if not selected:
            return
        category = self._category_ids.get(str(selected[0]))
        if not category:
            return
        self.category_var.set(category)
        self._active_group = None
        self._refresh_master_list()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self._refresh_master_list()

    def _display(self, value: Any) -> str:
        return mask_personal(value, self._personal_tokens) if self.hide_sensitive_var.get() else str(value or "")

    def _toggle_privacy(self) -> None:
        self.search_entry.configure(show="*" if self.hide_sensitive_var.get() else "")
        previous_key = str((self._active_group or {}).get("group_key") or "")
        previous_record = self._selected_detail_id()
        self._refresh_master_list(preferred_group_key=previous_key)
        for item_id, record_id in self._detail_record_ids.items():
            if record_id == previous_record and self.detail_tree is not None:
                self.detail_tree.selection_set(item_id)
                self.detail_tree.focus(item_id)
                self._update_action_state()
                break

    def _refresh_master_list(self, preferred_group_key: str = "") -> None:
        category = self.category_var.get() or "accounts_all"
        groups = filter_logical_groups(self._logical_groups.get(category, []), self.search_var.get())
        self.master_tree.delete(*self.master_tree.get_children())
        self._group_items.clear()
        preferred_item = ""
        for index, group in enumerate(groups):
            item_id = f"logical-{index}"
            self._group_items[item_id] = group
            self.master_tree.insert(
                "",
                "end",
                iid=item_id,
                text=_ui_text(self._display(group.get("title") or "未命名记录")),
                values=(_ui_text(self._display(group.get("subtitle") or "")),),
            )
            if preferred_group_key and group.get("group_key") == preferred_group_key:
                preferred_item = item_id
        chosen = preferred_item or next(iter(self._group_items), "")
        if chosen:
            self.master_tree.selection_set(chosen)
            self.master_tree.focus(chosen)
            self.master_tree.see(chosen)
            self._active_group = self._group_items[chosen]
            self._render_active_group()
        else:
            self._active_group = None
            self._render_empty_detail()
        physical = sum(len(group.get("records", [])) for group in groups)
        if _ENGLISH_UI:
            suffix = (
                "Passwords and technical IDs are hidden by default."
                if category.startswith("accounts_")
                else "Technical IDs are hidden by default."
            )
            self.status_var.set(
                f"{CATEGORY_LABELS.get(category, 'Records')}: {len(groups)} logical records, "
                f"{physical} source records. {suffix} Press F5 to refresh."
            )
        else:
            suffix = "密码和技术标识默认隐藏。" if category.startswith("accounts_") else "技术标识默认隐藏。"
            self.status_var.set(
                f"{CATEGORY_LABELS.get(category, '记录')}：{len(groups)} 条逻辑记录，"
                f"对应 {physical} 条底层记录。{suffix} 按 F5 刷新。"
            )

    def _on_group_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selected = self.master_tree.selection()
        self._active_group = self._group_items.get(str(selected[0])) if selected else None
        self._render_active_group()

    def _clear_detail_frame(self) -> None:
        for child in self.detail_frame.winfo_children():
            child.destroy()
        self._detail_record_ids.clear()
        self.detail_tree = None

    def _render_empty_detail(self) -> None:
        self._clear_detail_frame()
        self.detail_title_var.set(_ui_text("请选择一条记录"))
        self.detail_subtitle_var.set(_ui_text("左侧列表仅做逻辑分组，不会改写底层数据。"))
        ttk.Label(
            self.detail_frame,
            text="可先选择分类，再搜索网站、邮箱、账号或期刊名称。",
            foreground="#666",
            wraplength=520,
        ).grid(row=0, column=0, sticky="nw", pady=12)
        self._rebuild_more_menu(None)
        self._update_action_state()
        _translate_widget_tree(self.detail_frame)

    def _add_detail_row(self, row: int, label: str, value: Any) -> int:
        ttk.Label(self.detail_frame, text=_ui_text(label), foreground="#555").grid(
            row=row,
            column=0,
            sticky="nw",
            padx=(0, 14),
            pady=5,
        )
        ttk.Label(
            self.detail_frame,
            text=_ui_text(self._display(value or "—")),
            wraplength=430,
            justify=tk.LEFT,
        ).grid(row=row, column=1, sticky="nw", pady=5)
        return row + 1

    def _render_active_group(self) -> None:
        group = self._active_group
        if not group:
            self._render_empty_detail()
            return
        self._clear_detail_frame()
        self.detail_frame.columnconfigure(1, weight=1)
        kind = str(group.get("kind") or "")
        records = list(group.get("records") or [])
        self.detail_title_var.set(_ui_text(self._display(group.get("title") or "未命名记录")))
        self.detail_subtitle_var.set(_ui_text(self._display(group.get("subtitle") or "")))

        row = 0
        if kind == "account":
            row = self._add_detail_row(
                row,
                "账号分类",
                _ui_text(ACCOUNT_SCOPE_LABELS.get(str(group.get("scope")), "未分类账号")),
            )
            row = self._add_detail_row(row, "账号或邮箱", group.get("username"))
            row = self._add_detail_row(row, "登录网址", group.get("login_url"))
            row = self._add_detail_row(row, "状态", _account_status_label(group.get("status")))
            sources = {
                "legacy": "旧稳定投稿加密库（只读）",
                "vault": "新 Windows 加密账号库",
                "smart": "智能投稿兼容记录",
            }
            separator = ", " if _ENGLISH_UI else "、"
            source_text = separator.join(
                _ui_text(sources.get(str(value), str(value)))
                for value in group.get("sources", [])
            )
            row = self._add_detail_row(row, "记录来源", source_text)
        elif kind == "site":
            row = self._add_detail_row(row, "站点状态", _status_label(group.get("status")))
            row = self._add_detail_row(row, "登录网址", group.get("login_url"))
            row = self._add_detail_row(row, "投稿网址", group.get("submission_url"))
            journal_names = [str(item.get("journal_name") or "") for item in group.get("journals", [])]
            separator = ", " if _ENGLISH_UI else "、"
            row = self._add_detail_row(
                row,
                "关联期刊",
                separator.join(name for name in journal_names if name) or _ui_text("暂无"),
            )
        else:
            row = self._add_detail_row(row, "投稿站点", group.get("system_label"))
            row = self._add_detail_row(row, "状态", _status_label(group.get("status")))
            row = self._add_detail_row(row, "投稿网址", group.get("submission_url"))

        if len(records) > 1:
            ttk.Label(
                self.detail_frame,
                text=(
                    f"此处把 {len(records)} 条相符记录合并显示；底层记录没有迁移、合并或删除。"
                    "编辑、查看密码或删除具体记录前，请先打开“显示技术信息”并确认选择。"
                ),
                foreground="#7a5200",
                wraplength=520,
            ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 6))
            row += 1
        if kind == "account" and any(record.get("source_kind") == "smart" for record in records):
            ttk.Label(
                self.detail_frame,
                text="旧 Smart 明文记录可迁移到 Windows 当前用户加密库；迁移后运行态只保存加密引用。",
                foreground="#7a5200",
                wraplength=520,
            ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
            row += 1
        if kind == "account" and any(record.get("source_kind") == "vault" for record in records):
            ttk.Label(
                self.detail_frame,
                text="已安全保存，可由本地可信凭据代理自动登录；密码不会暴露给 Codex SDK 会话。",
                foreground="#28612f",
                wraplength=520,
            ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 8))
            row += 1

        ttk.Separator(self.detail_frame, orient=tk.HORIZONTAL).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(10, 10),
        )
        row += 1
        ttk.Label(self.detail_frame, text="底层记录", font=("TkDefaultFont", 10, "bold")).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
        )
        row += 1
        ttk.Label(
            self.detail_frame,
            text="编辑、查看密码或删除前，请先确认下面选中的具体来源。",
            foreground="#666",
            wraplength=520,
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(3, 6))
        row += 1

        columns = ("source", "status", "storage", "technical")
        self.detail_tree = ttk.Treeview(
            self.detail_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=min(max(len(records), 2), 6),
        )
        self.detail_tree.heading("source", text=_ui_text("来源"))
        self.detail_tree.heading("status", text=_ui_text("状态"))
        self.detail_tree.heading("storage", text=_ui_text("保存方式"))
        self.detail_tree.heading("technical", text=_ui_text("内部标识"))
        show_technical = self.show_technical_var.get()
        self.detail_tree.column("source", width=105 if show_technical else 150, minwidth=75, stretch=True)
        self.detail_tree.column("status", width=80 if show_technical else 100, minwidth=65, stretch=True)
        self.detail_tree.column("storage", width=115 if show_technical else 150, minwidth=85, stretch=True)
        technical_width = 155 if show_technical else 0
        self.detail_tree.column(
            "technical",
            width=technical_width,
            minwidth=technical_width,
            stretch=bool(technical_width),
        )
        preferred_item = ""
        for index, record in enumerate(records):
            item_id = f"detail-{index}"
            if kind == "account":
                record_id = str(record.get("record_key") or "")
                source_kind = str(record.get("source_kind") or "")
                source = (
                    "旧稳定投稿库（只读）"
                    if source_kind == "legacy"
                    else "新 Windows 加密库"
                    if source_kind == "vault"
                    else "智能投稿兼容"
                )
                storage = STORAGE_LABELS.get(str(record.get("password_storage") or "none"), "未知")
                state = _account_status_label(record.get("status"))
                technical = str(record.get("account_id") or "")
                if not preferred_item and not bool(record.get("read_only")):
                    preferred_item = item_id
            elif kind == "site":
                record_id = str(record.get("system_id") or "")
                source = "投稿站点记录"
                storage = "—"
                state = _status_label(record.get("status"))
                technical = record_id
            else:
                record_id = str(record.get("journal_key") or "")
                source = "期刊记录"
                storage = "—"
                state = _status_label(record.get("status"))
                technical = record_id
            self._detail_record_ids[item_id] = record_id
            self.detail_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(source, state, storage, self._display(technical) if show_technical else ""),
            )
        self.detail_tree.grid(row=row, column=0, columnspan=2, sticky="ew")
        self.detail_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_action_state())
        first = preferred_item or next(iter(self._detail_record_ids), "")
        if first:
            self.detail_tree.selection_set(first)
            self.detail_tree.focus(first)
        if show_technical:
            row += 1
            ttk.Label(
                self.detail_frame,
                text="内部标识只用于排错；日常使用无需查看或修改。",
                foreground="#666",
                wraplength=520,
            ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._rebuild_more_menu(kind)
        self._update_action_state()
        _translate_widget_tree(self.detail_frame)

    def _selected_detail_id(self) -> str:
        tree = getattr(self, "detail_tree", None)
        if tree is None:
            return ""
        selected = tree.selection()
        return self._detail_record_ids.get(str(selected[0]), "") if selected else ""

    def _new_for_current_category(self) -> None:
        category = self.category_var.get()
        if category.startswith("accounts_"):
            self.new_account()
        elif category == "sites":
            self.new_system()
        else:
            self.new_journal()

    def _edit_selected_record(self) -> None:
        if not self._confirm_specific_record_selection():
            return
        kind = str((self._active_group or {}).get("kind") or "")
        if kind == "account":
            self.edit_account()
        elif kind == "site":
            self.edit_system()
        elif kind == "journal":
            self.edit_journal()

    def _delete_selected_record(self) -> None:
        if not self._confirm_specific_record_selection():
            return
        kind = str((self._active_group or {}).get("kind") or "")
        if kind == "account":
            self.delete_account()
        elif kind == "site":
            self.delete_system()
        elif kind == "journal":
            self.delete_journal()

    def _rebuild_more_menu(self, kind: str | None) -> None:
        self.more_menu.delete(0, "end")
        if kind == "account":
            self.more_menu.add_command(label=_ui_text("临时查看密码"), command=self.view_password)
            self.more_menu.add_command(
                label=_ui_text("迁移选中旧明文到 Windows 加密库"),
                command=self.copy_smart_password_to_vault,
            )
            self.more_menu.add_command(
                label=_ui_text("迁移全部旧明文账号"),
                command=self.migrate_all_smart_passwords,
            )
            self.more_menu.add_separator()
            self.more_menu.add_command(
                label=_ui_text("删除所选底层记录…"),
                command=self._delete_selected_record,
            )
            self.more_menu.add_separator()
            self.more_menu.add_command(
                label=_ui_text("检查账号重复"),
                command=lambda: self.show_duplicates("accounts"),
            )
        elif kind == "site":
            self.more_menu.add_command(
                label=_ui_text("删除所选站点记录…"),
                command=self._delete_selected_record,
            )
            self.more_menu.add_separator()
            self.more_menu.add_command(
                label=_ui_text("检查站点重复"),
                command=lambda: self.show_duplicates("systems"),
            )
        elif kind == "journal":
            self.more_menu.add_command(
                label=_ui_text("删除所选期刊记录…"),
                command=self._delete_selected_record,
            )
            self.more_menu.add_separator()
            self.more_menu.add_command(
                label=_ui_text("检查期刊重复"),
                command=lambda: self.show_duplicates("journals"),
            )
        else:
            self.more_menu.add_command(label=_ui_text("请先选择记录"), state="disabled")

    def _selection_requires_technical_info(self) -> bool:
        records = list((self._active_group or {}).get("records") or [])
        return len(records) > 1 and not self.show_technical_var.get()

    def _confirm_specific_record_selection(self) -> bool:
        if not self._selection_requires_technical_info():
            return True
        _showinfo(
            "请先确认底层记录",
            "当前逻辑记录包含多条底层记录。请先打开“显示技术信息”，再选择要操作的具体记录。",
            parent=self,
        )
        return False

    def _update_action_state(self) -> None:
        record_id = self._selected_detail_id()
        kind = str((self._active_group or {}).get("kind") or "")
        read_only = bool(kind == "account" and record_id and self.store.is_account_read_only(record_id))
        requires_technical = self._selection_requires_technical_info()
        editable = bool(record_id) and not read_only and not requires_technical
        self.edit_button.state(["!disabled"] if editable else ["disabled"])
        account_category = self.category_var.get().startswith("accounts_")
        can_add = not (account_category and self.store.vault_read_only)
        self.add_button.state(["!disabled"] if can_add else ["disabled"])
        self.more_button.state(["!disabled"] if self._active_group else ["disabled"])
        try:
            if kind == "account":
                view_state = "normal" if record_id and not requires_technical else "disabled"
                delete_state = "disabled" if read_only or not record_id or requires_technical else "normal"
                self.more_menu.entryconfigure(0, state=view_state)
                self.more_menu.entryconfigure(3, state=delete_state)
            elif kind in {"site", "journal"}:
                delete_state = "normal" if record_id and not requires_technical else "disabled"
                self.more_menu.entryconfigure(0, state=delete_state)
        except tk.TclError:
            pass

    def _show_legacy_vault_read_only(self) -> None:
        _showinfo(
            "账号库安全保护",
            "旧稳定投稿库始终只读。当前新账号库路径与旧库冲突，因此新增和改写也已安全停用。",
            parent=self,
        )

    @staticmethod
    def _account_fields(
        *,
        include_technical: bool = False,
    ) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = [
            {
                "key": "scope",
                "label": "账号分类",
                "kind": "combo",
                "values": list(SCOPE_LABEL_TO_VALUE)[1:],
                "readonly": True,
                "value_to_label": SCOPE_VALUE_TO_LABEL,
                "label_to_value": {
                    label: value
                    for label, value in list(SCOPE_LABEL_TO_VALUE.items())[1:]
                },
            },
            {"key": "provider", "label": "网站/系统"},
            {"key": "login_url", "label": "登录网址"},
            {"key": "username", "label": "账号"},
            {"key": "email", "label": "邮箱"},
            {"key": "purpose", "label": "用途"},
            {"key": "related_journals", "label": "关联期刊"},
            {
                "key": "status",
                "label": "登录状态",
                "kind": "combo",
                "values": [
                    label
                    for value, label in UI_STATUS_LABELS.items()
                    if value != "source_status_conflict"
                ],
                "value_to_label": UI_STATUS_LABELS,
                "label_to_value": {label: value for value, label in UI_STATUS_LABELS.items()},
            },
            {"key": "notes", "label": "备注", "kind": "multiline", "height": 4},
        ]
        if include_technical:
            fields.insert(
                0,
                {"key": "account_id", "label": "内部标识（高级）", "readonly": True},
            )
        return fields

    def new_account(self) -> None:
        if self.store.vault_read_only:
            self._show_legacy_vault_read_only()
            return
        record = {"scope": "journal_system", "status": "unknown"}
        dialog = _RecordDialog(
            self,
            title="新增加密账号",
            record=record,
            fields=self._account_fields(),
            allow_password_input=True,
        )
        result = dialog.show()
        if result is None:
            return
        password = str(result.pop("_new_password", ""))
        try:
            self.store.save_account(result, password=password or None)
        except Exception as exc:
            self._show_error("账号保存失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()
        saved_subject = "账号和密码" if password else "账号信息（未保存密码）"
        _showinfo(
            "账号已安全保存",
            f"{saved_subject}已使用 Windows 当前用户加密库保存。\n\n"
            "可信本地密码填充代理尚未就绪，因此该记录暂不可用于自动填充。",
            parent=self,
        )

    def edit_account(self) -> None:
        record_key = self._selected_detail_id()
        if not record_key:
            return
        if self.store.is_account_read_only(record_key):
            self._show_legacy_vault_read_only()
            return
        try:
            record = self.store.get_account(record_key)
        except Exception as exc:
            self._show_error("账号读取失败", exc)
            return
        source = str(record.get("source_kind") or "")
        dialog = _RecordDialog(
            self,
            title="编辑新加密账号" if source == "vault" else "编辑智能投稿兼容记录",
            record=record,
            fields=self._account_fields(
                include_technical=self.show_technical_var.get(),
            ),
            allow_password_input=source == "vault",
            password_hint=(
                "留空表示保持原密码；填写后使用 Windows 当前用户加密覆盖。"
                if source == "vault"
                else ""
            ),
        )
        result = dialog.show()
        if result is None:
            return
        password = str(result.pop("_new_password", ""))
        try:
            self.store.save_account(result, original_id=record_key, password=password or None)
        except Exception as exc:
            self._show_error("账号保存失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    def delete_account(self) -> None:
        record_key = self._selected_detail_id()
        if not record_key:
            return
        if self.store.is_account_read_only(record_key):
            self._show_legacy_vault_read_only()
            return
        source = "新加密账号库" if record_key.startswith("vault:") else "智能投稿兼容库"
        if not _askyesno(
            "确认删除账号",
            (
                f"确定从{source}删除当前选中的这条底层记录吗？\n\n"
                "逻辑分组中的其他记录不会被删除。删除前会创建时间戳备份。"
            ),
            parent=self,
        ):
            return
        try:
            self.store.delete_account(record_key)
        except Exception as exc:
            self._show_error("账号删除失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    def view_password(self) -> None:
        if not self._confirm_specific_record_selection():
            return
        record_key = self._selected_detail_id()
        if not record_key:
            return
        try:
            password = self.store.reveal_password(record_key)
        except Exception as exc:
            self._show_error("密码读取失败", exc)
            return
        if not password:
            _showinfo("未保存密码", "所选账号没有可读取的密码。", parent=self)
            return
        _PasswordViewer(self, password, compatibility_plaintext=record_key.startswith("smart:"))
        password = ""

    def copy_smart_password_to_vault(self) -> None:
        if not self._confirm_specific_record_selection():
            return
        record_key = self._selected_detail_id()
        if not record_key.startswith("smart:"):
            _showinfo(
                "无需迁移",
                "请选择来源为“智能投稿兼容”的旧明文账号记录。",
                parent=self,
            )
            return
        if not _askyesno(
            "迁移账号密码",
            (
                "程序会先把密码写入 Windows 当前用户 DPAPI 加密库，"
                "确认加密写入完成后再删除旧明文。\n\n"
                "是否继续？"
            ),
            parent=self,
        ):
            return
        try:
            self.store.copy_smart_account_to_vault(record_key)
        except Exception as exc:
            self._show_error("密码迁移失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()
        _showinfo(
            "迁移完成",
            "密码已加密保存，旧 Smart 明文字段已清理，可用于本地自动登录。",
            parent=self,
        )

    def migrate_all_smart_passwords(self) -> None:
        if not _askyesno(
            "确认迁移全部旧明文账号",
            "将逐条加密并清理所有 Smart 兼容明文密码。每条记录都会保留事务备份和审计，是否继续？",
            parent=self,
        ):
            return
        try:
            result = self.store.migrate_smart_plaintext_credentials()
        except Exception as exc:
            self._show_error("批量迁移失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()
        _showinfo(
            "批量迁移完成",
            (
                f"已迁移：{result.get('migrated_count', 0)} 条\n"
                f"仍有明文：{result.get('plaintext_remaining', 0)} 条\n"
                f"失败：{len(result.get('failed', {}))} 条"
            ),
            parent=self,
        )

    @staticmethod
    def _system_fields(*, include_technical: bool = False) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = [
            {"key": "label", "label": "投稿站点名称"},
            {
                "key": "status",
                "label": "状态",
                "kind": "combo",
                "values": [
                    label
                    for value, label in UI_STATUS_LABELS.items()
                    if value != "source_status_conflict"
                ],
                "value_to_label": UI_STATUS_LABELS,
                "label_to_value": {label: value for value, label in UI_STATUS_LABELS.items()},
            },
            {"key": "login_url", "label": "登录网址"},
            {"key": "submission_url", "label": "投稿网址"},
            {"key": "status_url_template", "label": "稿件状态网址模板"},
            {"key": "email_senders", "label": "通知邮件发件人", "kind": "list", "height": 3},
            {"key": "notes", "label": "备注", "kind": "multiline", "height": 5},
        ]
        if include_technical:
            fields.insert(0, {"key": "system_id", "label": "内部标识（高级）", "readonly": True})
        return fields

    def new_system(self) -> None:
        dialog = _RecordDialog(
            self,
            title="新增投稿站点",
            record={"status": "unknown"},
            fields=self._system_fields(),
        )
        result = dialog.show()
        if result is not None:
            self._save_system_result(result)

    def edit_system(self) -> None:
        system_id = self._selected_detail_id()
        if not system_id:
            return
        try:
            record = self.store.get_system(system_id)
        except Exception as exc:
            self._show_error("投稿站点读取失败", exc)
            return
        dialog = _RecordDialog(
            self,
            title="编辑投稿站点",
            record=record,
            fields=self._system_fields(include_technical=self.show_technical_var.get()),
        )
        result = dialog.show()
        if result is not None:
            self._save_system_result(result, original_id=system_id)

    def _save_system_result(self, result: dict[str, Any], original_id: str | None = None) -> None:
        result.pop("_new_password", None)
        try:
            self.store.save_system(result, original_id=original_id)
        except Exception as exc:
            self._show_error("投稿站点保存失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    def delete_system(self) -> None:
        system_id = self._selected_detail_id()
        if not system_id:
            return
        if not _askyesno(
            "确认删除投稿站点",
            "确定删除当前选中的这条站点底层记录吗？删除前会创建时间戳备份。",
            parent=self,
        ):
            return
        try:
            self.store.delete_system(system_id)
        except Exception as exc:
            self._show_error("投稿站点删除失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    @staticmethod
    def _journal_fields(
        system_labels: list[str],
        system_value_to_label: dict[str, str],
        system_label_to_value: dict[str, str],
        *,
        include_technical: bool = False,
    ) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = [
            {"key": "journal_name", "label": "期刊名称"},
            {
                "key": "system_id",
                "label": "投稿站点",
                "kind": "combo",
                "values": system_labels,
                "readonly": True,
                "value_to_label": system_value_to_label,
                "label_to_value": system_label_to_value,
            },
            {
                "key": "status",
                "label": "状态",
                "kind": "combo",
                "values": [
                    label
                    for value, label in UI_STATUS_LABELS.items()
                    if value != "source_status_conflict"
                ],
                "value_to_label": UI_STATUS_LABELS,
                "label_to_value": {label: value for value, label in UI_STATUS_LABELS.items()},
            },
            {"key": "submission_url", "label": "投稿网址"},
            {"key": "linked_papers", "label": "关联论文", "kind": "list", "height": 4},
            {"key": "source", "label": "来源"},
        ]
        if include_technical:
            fields.insert(0, {"key": "journal_key", "label": "内部标识（高级）", "readonly": True})
        return fields

    def _system_choices(self) -> tuple[list[str], dict[str, str], dict[str, str]]:
        systems = self.store.list_systems()
        totals: dict[str, int] = {}
        for system in systems:
            label = str(system.get("label") or "未命名投稿站点").strip()
            totals[label] = totals.get(label, 0) + 1
        seen: dict[str, int] = {}
        value_to_label: dict[str, str] = {}
        label_to_value: dict[str, str] = {}
        for system in systems:
            system_id = str(system.get("system_id") or "").strip()
            if not system_id:
                continue
            label = str(system.get("label") or "未命名投稿站点").strip()
            seen[label] = seen.get(label, 0) + 1
            display = label if totals[label] == 1 else f"{label}（记录 {seen[label]}）"
            value_to_label[system_id] = display
            label_to_value[display] = system_id
        return sorted(label_to_value), value_to_label, label_to_value

    def new_journal(self) -> None:
        try:
            labels, value_to_label, label_to_value = self._system_choices()
            fields = self._journal_fields(labels, value_to_label, label_to_value)
        except Exception as exc:
            self._show_error("投稿站点读取失败", exc)
            return
        dialog = _RecordDialog(self, title="新增期刊", record={"status": "unknown"}, fields=fields)
        result = dialog.show()
        if result is not None:
            self._save_journal_result(result)

    def edit_journal(self) -> None:
        journal_key = self._selected_detail_id()
        if not journal_key:
            return
        try:
            record = self.store.get_journal(journal_key)
            labels, value_to_label, label_to_value = self._system_choices()
            fields = self._journal_fields(
                labels,
                value_to_label,
                label_to_value,
                include_technical=self.show_technical_var.get(),
            )
        except Exception as exc:
            self._show_error("期刊记录读取失败", exc)
            return
        dialog = _RecordDialog(self, title="编辑期刊", record=record, fields=fields)
        result = dialog.show()
        if result is not None:
            self._save_journal_result(result, original_id=journal_key)

    def _save_journal_result(self, result: dict[str, Any], original_id: str | None = None) -> None:
        result.pop("_new_password", None)
        try:
            self.store.save_journal(result, original_id=original_id)
        except Exception as exc:
            self._show_error("期刊记录保存失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    def delete_journal(self) -> None:
        journal_key = self._selected_detail_id()
        if not journal_key:
            return
        if not _askyesno(
            "确认删除期刊",
            "确定删除当前选中的这条期刊底层记录吗？删除前会创建时间戳备份。",
            parent=self,
        ):
            return
        try:
            self.store.delete_journal(journal_key)
        except Exception as exc:
            self._show_error("期刊记录删除失败", exc)
            return
        self.refresh_all(show_error=False)
        self._notify_changed()

    def show_duplicates(self, record_type: str) -> None:
        try:
            groups = self.store.duplicate_report().get(record_type, [])
        except Exception as exc:
            self._show_error("重复检查失败", exc)
            return
        if not groups:
            _showinfo("重复检查", "没有发现潜在重复记录。", parent=self)
            return
        lines = [f"{index}. {', '.join(group)}" for index, group in enumerate(groups, 1)]
        _showwarning(
            "发现潜在重复",
            f"共发现 {len(groups)} 组。这里只列出记录标识，不显示密码：\n\n" + "\n".join(lines[:20]),
            parent=self,
        )


def open_account_site_manager(
    parent: tk.Misc,
    credential_path: Path | str = DEFAULT_CREDENTIAL_PATH,
    site_registry_path: Path | str = DEFAULT_SITE_REGISTRY_PATH,
    *,
    vault_path: Path | str | None = DEFAULT_ACCOUNT_VAULT_PATH,
    legacy_vault_path: Path | str = LEGACY_CREDENTIAL_VAULT_PATH,
    backup_dir: Path | str | None = None,
    audit_path: Path | str | None = None,
    on_change: Callable[[], None] | None = None,
    store: AccountSiteStore | None = None,
) -> AccountSiteManager:
    """Open or focus the shared manager; suitable for ``UnifiedControlPanel``."""
    attribute = "_account_site_manager_window"
    existing = getattr(parent, attribute, None)
    if isinstance(existing, AccountSiteManager):
        try:
            if existing.winfo_exists():
                existing.hide_sensitive_var.set(True)
                existing._toggle_privacy()
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return existing
        except tk.TclError:
            pass
    window = AccountSiteManager(
        parent,
        credential_path=credential_path,
        site_registry_path=site_registry_path,
        vault_path=vault_path,
        legacy_vault_path=legacy_vault_path,
        backup_dir=backup_dir,
        audit_path=audit_path,
        on_change=on_change,
        store=store,
    )
    setattr(parent, attribute, window)

    def clear_reference(event: tk.Event[tk.Misc]) -> None:
        if event.widget is window and getattr(parent, attribute, None) is window:
            setattr(parent, attribute, None)

    window.bind("<Destroy>", clear_reference, add="+")
    return window


def main() -> int:
    """Run the account/site manager as an independent business window."""

    root = tk.Tk()
    root.withdraw()
    window = open_account_site_manager(root)
    # ``open_account_site_manager`` normally attaches the window to a visible
    # workbench parent.  In standalone mode the hidden root must not remain its
    # transient owner, otherwise Windows keeps the manager itself hidden.
    try:
        window.wm_transient("")
    except tk.TclError:
        pass
    window.deiconify()
    window.lift()
    window.focus_force()

    def close_window() -> None:
        try:
            window.destroy()
        finally:
            root.destroy()

    window.protocol("WM_DELETE_WINDOW", close_window)
    root.mainloop()
    return 0
