from __future__ import annotations

import inspect
import json
import re
import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from tkinter import messagebox, ttk
from typing import Any
from urllib.parse import urlsplit

from unified_paper_orchestrator_v2.provider_runtime import responses_endpoint_url

from .presentation import responsive_window_size


API_KEY_MODE = "api_key"
LEGACY_CODEX_MODE = "legacy_codex"

CAPABILITY_LABELS = {
    "untested": "未检测",
    "compatible": "已兼容",
    "incompatible": "不兼容",
    "error": "检测失败",
}

REASONING_EFFORTS = ("", "minimal", "low", "medium", "high", "xhigh")


class AIExecutionSettingsError(ValueError):
    """A safe, user-facing validation error for the settings window."""


def _record_dict(value: Any) -> dict[str, Any]:
    """Return a shallowly editable record for dict or dataclass store results."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        return dict(converted) if isinstance(converted, dict) else {}
    try:
        return dict(vars(value))
    except (TypeError, ValueError):
        return {}


def _profile_list(value: Any) -> list[dict[str, Any]]:
    if value is None or isinstance(value, (str, bytes, Mapping)):
        return []
    if not isinstance(value, Sequence):
        try:
            value = list(value)
        except TypeError:
            return []
    return [_record_dict(item) for item in value]


def _parse_custom_headers(text: str) -> dict[str, str]:
    raw = text.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIExecutionSettingsError("自定义请求头必须是 JSON 对象。") from exc
    if not isinstance(value, dict):
        raise AIExecutionSettingsError("自定义请求头必须是 JSON 对象。")
    result: dict[str, str] = {}
    for key, item in value.items():
        clean_key = str(key).strip()
        if not clean_key or not isinstance(item, str):
            raise AIExecutionSettingsError("自定义请求头的名称和值必须是字符串。")
        result[clean_key] = item
    return result


_SAFE_DIAGNOSTIC_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_ERROR_MODULE_PREFIXES = (
    "unified_paper_orchestrator.ai_execution",
    "unified_paper_orchestrator.ai_execution_policy",
    "unified_paper_orchestrator.ai_provider_store",
    "unified_paper_orchestrator.ui.ai_execution_settings",
)


def _safe_request_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return raw


def _format_error_for_display(exc: BaseException) -> str:
    """Format only trusted, value-free diagnostics for the settings dialog."""

    module_name = str(type(exc).__module__ or "")
    type_name = type(exc).__name__
    trusted_message = module_name.startswith(_SAFE_ERROR_MODULE_PREFIXES)
    message = str(exc).strip() if trusted_message else f"操作未完成（{type_name}）。"
    lines = [message or f"操作未完成（{type_name}）。"]

    code = str(getattr(exc, "code", "") or "").strip()
    if _SAFE_DIAGNOSTIC_TOKEN.fullmatch(code):
        lines.append(f"错误代码：{code}")

    details = getattr(exc, "details", None)
    if not isinstance(details, Mapping) or module_name != "unified_paper_orchestrator.ai_execution":
        return "\n".join(lines)
    request_url = _safe_request_url(details.get("request_url"))
    if request_url:
        lines.append(f"实际请求地址：{request_url}")
    for key, label in (
        ("sdk_status", "SDK 状态"),
        ("blocker_code", "阻塞代码"),
        ("failure_type", "异常类型"),
    ):
        value = str(details.get(key) or "").strip()
        if _SAFE_DIAGNOSTIC_TOKEN.fullmatch(value):
            lines.append(f"{label}：{value}")
    for key, label in (("token_budget", "本地检测预算"), ("total_tokens", "实际总 token")):
        value = details.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9:
            lines.append(f"{label}：{value}")
    return "\n".join(lines)


class AIExecutionSettingsService:
    """Small UI adapter around the future ``AIProviderStore`` contract."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        settings = _record_dict(self.store.load_settings())
        profiles = _profile_list(self.store.list_profiles())
        return settings, profiles

    @staticmethod
    def profile_is_valid(profile: Mapping[str, Any]) -> bool:
        return all(
            str(profile.get(field) or "").strip()
            for field in ("profile_id", "display_name", "base_url", "default_model")
        )

    @staticmethod
    def profile_is_compatible(profile: Mapping[str, Any]) -> bool:
        return str(profile.get("capability_status") or "untested").strip().casefold() == "compatible"

    def has_api_key(self, profile_id: str) -> bool:
        return bool(self.store.has_api_key(profile_id))

    @staticmethod
    def _call_settings_mutator(method: Callable[..., Any], value: str, expected_revision: Any) -> Any:
        """Pass CAS revision when the installed store exposes that newer contract."""
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_revision = any(
            parameter.name == "expected_revision" or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_revision:
            return method(value, expected_revision=expected_revision)
        return method(value)

    def save_profile(
        self,
        *,
        profile_id: str,
        expected_revision: Any,
        values: Mapping[str, Any],
        api_key: str,
    ) -> dict[str, Any]:
        payload = {
            "display_name": str(values.get("display_name") or "").strip(),
            "base_url": str(values.get("base_url") or "").strip(),
            "wire_api": str(values.get("wire_api") or "responses").strip() or "responses",
            "default_model": str(values.get("default_model") or "").strip(),
            "reasoning_effort": str(values.get("reasoning_effort") or "").strip(),
            "proxy_url": str(values.get("proxy_url") or "").strip(),
            "custom_headers": dict(values.get("custom_headers") or {}),
        }
        missing_labels = [
            label
            for field, label in (
                ("display_name", "名称"),
                ("base_url", "API 地址"),
                ("default_model", "模型"),
            )
            if not payload[field]
        ]
        if missing_labels:
            raise AIExecutionSettingsError("请填写：" + "、".join(missing_labels) + "。")

        if profile_id:
            saved = self.store.update_profile(
                profile_id,
                expected_revision=expected_revision,
                **payload,
            )
        else:
            saved = self.store.create_profile(**payload)
        saved_record = _record_dict(saved)
        saved_id = str(saved_record.get("profile_id") or profile_id).strip()
        if not saved_id:
            raise AIExecutionSettingsError("配置已写入，但存储模块未返回配置标识。")
        if api_key.strip():
            self.store.set_api_key(saved_id, api_key)
        return saved_record

    def delete_profile(self, profile_id: str, expected_revision: Any) -> None:
        if not profile_id:
            raise AIExecutionSettingsError("请先选择要删除的配置。")
        self.store.delete_profile(profile_id, expected_revision=expected_revision)

    def save_mode(
        self,
        mode: str,
        selected_profile_id: str,
        profiles: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        current_settings = _record_dict(self.store.load_settings())
        if mode == LEGACY_CODEX_MODE:
            saved = self._call_settings_mutator(
                self.store.set_execution_mode,
                LEGACY_CODEX_MODE,
                current_settings.get("revision"),
            )
            return _record_dict(saved) or _record_dict(self.store.load_settings())
        if mode != API_KEY_MODE:
            raise AIExecutionSettingsError("未识别的 AI 执行模式。")

        profile = next(
            (
                item
                for item in profiles
                if str(item.get("profile_id") or "").strip() == selected_profile_id
            ),
            None,
        )
        if profile is None:
            raise AIExecutionSettingsError("API 密钥模式需要先选择一个配置。")
        if not self.profile_is_valid(profile):
            raise AIExecutionSettingsError("所选 API 配置不完整，请先补齐名称、API 地址和模型。")
        if not self.has_api_key(selected_profile_id):
            raise AIExecutionSettingsError("所选 API 配置尚未安全保存密钥。")
        capability_status = str(profile.get("capability_status") or "untested").strip().casefold()
        if not self.profile_is_compatible(profile):
            message = {
                "untested": "所选 API 配置尚未完成兼容性检测。",
                "incompatible": "所选 API 配置与当前执行协议不兼容。",
                "error": "所选 API 配置上次兼容性检测失败。",
            }.get(capability_status, "所选 API 配置的兼容状态无效。")
            raise AIExecutionSettingsError(message)

        # The active profile must be visible to the store before it validates
        # entry into API mode.  Runtime switch safety remains store-owned.
        active_settings = self._call_settings_mutator(
            self.store.set_active_profile,
            selected_profile_id,
            current_settings.get("revision"),
        )
        active_record = _record_dict(active_settings) or _record_dict(self.store.load_settings())
        saved = self._call_settings_mutator(
            self.store.set_execution_mode,
            API_KEY_MODE,
            active_record.get("revision"),
        )
        return _record_dict(saved) or _record_dict(self.store.load_settings())


def _default_store_factory() -> Any:
    # Deliberately delayed: V1 remains importable while the store module is
    # being introduced or omitted from a compatibility-only installation.
    from ..ai_provider_store import AIProviderStore

    return AIProviderStore()


class AIExecutionSettingsWindow(tk.Toplevel):
    """Settings window for choosing Codex-account or API-profile execution."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        store: Any | None = None,
        store_factory: Callable[[], Any] | None = None,
        test_callback: Callable[[Any, str], Any] | None = None,
        codex_account_service: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self.title("AI执行与API")
        width, height, minimum_width, minimum_height = responsive_window_size(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            preferred=(920, 660),
            minimum=(760, 560),
        )
        self.geometry(f"{width}x{height}")
        self.minsize(minimum_width, minimum_height)
        self.transient(parent.winfo_toplevel())

        self.test_callback = test_callback
        if codex_account_service is None:
            from ..codex_sdk_account import CodexSDKAccountService

            codex_account_service = CodexSDKAccountService()
        self.codex_account_service = codex_account_service
        self.service: AIExecutionSettingsService | None = None
        self._startup_error: BaseException | None = None
        try:
            resolved_store = store if store is not None else (store_factory or _default_store_factory)()
            self.service = AIExecutionSettingsService(resolved_store)
        except Exception as exc:
            self._startup_error = exc

        self.mode_var = tk.StringVar(value=API_KEY_MODE)
        self.status_var = tk.StringVar(value="")
        self.key_status_var = tk.StringVar(value="密钥：尚未保存")
        self.capability_var = tk.StringVar(value="兼容状态：未检测")
        self.request_url_var = tk.StringVar(value="实际请求地址：-")
        self.codex_account_status_var = tk.StringVar(value="Codex 账号：正在检测…")
        self.profile_vars = {
            "display_name": tk.StringVar(),
            "base_url": tk.StringVar(),
            "api_key": tk.StringVar(),
            "default_model": tk.StringVar(),
            "reasoning_effort": tk.StringVar(),
            "wire_api": tk.StringVar(value="responses"),
            "proxy_url": tk.StringVar(),
        }
        self.profile_vars["base_url"].trace_add("write", self._update_request_url)
        self._profiles: list[dict[str, Any]] = []
        self._profile_items: dict[str, str] = {}
        self._editing_profile_id = ""
        self._editing_revision: Any = None
        self._advanced_visible = False
        self._test_in_progress = False
        self._codex_account_in_progress = False
        self._interactive_widgets: list[tk.Widget] = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close_window)
        self.bind("<Escape>", lambda _event: self._close_window())
        self.after_idle(self.refresh)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        mode_frame = ttk.LabelFrame(outer, text="执行模式", padding=10)
        mode_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        mode_frame.columnconfigure(2, weight=1)
        api_radio = ttk.Radiobutton(
            mode_frame,
            text="API 密钥模式（当前仅审阅）",
            value=API_KEY_MODE,
            variable=self.mode_var,
            command=self._update_mode_visibility,
        )
        api_radio.grid(row=0, column=0, sticky="w", padx=(0, 22))
        codex_radio = ttk.Radiobutton(
            mode_frame,
            text="Codex 账号模式（完整流程）",
            value=LEGACY_CODEX_MODE,
            variable=self.mode_var,
            command=self._update_mode_visibility,
        )
        codex_radio.grid(row=0, column=1, sticky="w")
        self.save_mode_button = ttk.Button(mode_frame, text="保存模式", command=self._save_mode)
        self.save_mode_button.grid(row=0, column=3, sticky="e", padx=(12, 0))
        explanation = ttk.Label(
            mode_frame,
            text=(
                "API 密钥模式依赖有效配置、本机安全保存的密钥与兼容性检测；当前先支持连接检测和"
                "选中论文的第 7 步私有暂存审阅。Codex 账号模式用于完整流程：写论文、画图、选刊作者"
                "和智能投稿均由 EXE 内程序调度的本地 Codex SDK 工作流执行。API 模式下"
                "未迁移的入口会停止并提示，不会静默回退；两种模式都不会修改既有任务提示词。"
            ),
            foreground="#555",
            wraplength=840,
        )
        explanation.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        explanation.bind(
            "<Configure>",
            lambda event: explanation.configure(wraplength=max(420, event.width)),
        )
        self._interactive_widgets.extend((api_radio, codex_radio, self.save_mode_button))

        self.api_frame = ttk.LabelFrame(outer, text="API 配置", padding=10)
        self.api_frame.grid(row=1, column=0, sticky="nsew")
        self.api_frame.columnconfigure(0, weight=2)
        self.api_frame.columnconfigure(1, weight=3)
        self.api_frame.rowconfigure(0, weight=1)

        left = ttk.Frame(self.api_frame, padding=(0, 0, 10, 0))
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="配置列表").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.profile_tree = ttk.Treeview(
            left,
            columns=("status",),
            show="tree headings",
            selectmode="browse",
            height=12,
        )
        self.profile_tree.heading("#0", text="名称")
        self.profile_tree.heading("status", text="兼容状态")
        self.profile_tree.column("#0", width=190, anchor=tk.W)
        self.profile_tree.column("status", width=80, anchor=tk.W, stretch=False)
        self.profile_tree.grid(row=1, column=0, sticky="nsew")
        self.profile_tree.bind("<<TreeviewSelect>>", self._on_profile_selected)
        profile_actions = ttk.Frame(left)
        profile_actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.new_button = ttk.Button(profile_actions, text="新增", command=self._new_profile)
        self.new_button.pack(side=tk.LEFT)
        self.delete_button = ttk.Button(profile_actions, text="删除", command=self._delete_profile)
        self.delete_button.pack(side=tk.LEFT, padx=(8, 0))
        self._interactive_widgets.extend((self.profile_tree, self.new_button, self.delete_button))

        form = ttk.Frame(self.api_frame, padding=(12, 0, 0, 0))
        form.grid(row=0, column=1, sticky="nsew")
        form.columnconfigure(1, weight=1)
        form.rowconfigure(8, weight=1)
        row = 0
        row = self._add_entry_row(form, row, "名称", "display_name")
        row = self._add_entry_row(form, row, "API 地址", "base_url")
        ttk.Label(form, textvariable=self.request_url_var, foreground="#555").grid(
            row=row,
            column=1,
            sticky="w",
            pady=(0, 5),
        )
        row += 1
        row = self._add_entry_row(form, row, "API Key", "api_key", show="*")
        ttk.Label(form, textvariable=self.key_status_var, foreground="#555").grid(
            row=row,
            column=1,
            sticky="w",
            pady=(0, 5),
        )
        row += 1
        row = self._add_entry_row(form, row, "模型", "default_model")
        ttk.Label(form, text="推理等级").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        reasoning = ttk.Combobox(
            form,
            textvariable=self.profile_vars["reasoning_effort"],
            values=REASONING_EFFORTS,
            state="readonly",
        )
        reasoning.grid(row=row, column=1, sticky="ew", pady=5)
        self._interactive_widgets.append(reasoning)
        row += 1
        ttk.Label(form, textvariable=self.capability_var, foreground="#555").grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(4, 6),
        )
        row += 1

        self.advanced_button = ttk.Button(form, text="显示高级字段", command=self._toggle_advanced)
        self.advanced_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 4))
        self._interactive_widgets.append(self.advanced_button)
        row += 1
        self.advanced_frame = ttk.Frame(form)
        self.advanced_frame.grid(row=row, column=0, columnspan=2, sticky="nsew")
        self.advanced_frame.columnconfigure(1, weight=1)
        self.advanced_frame.rowconfigure(2, weight=1)
        advanced_row = 0
        advanced_row = self._add_entry_row(self.advanced_frame, advanced_row, "代理", "proxy_url")
        advanced_row = self._add_entry_row(self.advanced_frame, advanced_row, "接口协议", "wire_api")
        ttk.Label(self.advanced_frame, text="自定义请求头（JSON）").grid(
            row=advanced_row,
            column=0,
            sticky="nw",
            padx=(0, 10),
            pady=5,
        )
        self.custom_headers_text = tk.Text(self.advanced_frame, height=4, wrap="word")
        self.custom_headers_text.grid(row=advanced_row, column=1, sticky="nsew", pady=5)
        self._interactive_widgets.append(self.custom_headers_text)
        self.advanced_frame.grid_remove()
        row += 1

        actions = ttk.Frame(form)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.save_profile_button = ttk.Button(actions, text="保存", command=self._save_profile)
        self.save_profile_button.pack(side=tk.LEFT)
        self.test_button = ttk.Button(actions, text="测试连接", command=self._test_profile)
        self.test_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="关闭", command=self._close_window).pack(side=tk.RIGHT)
        self._interactive_widgets.extend((self.save_profile_button, self.test_button))

        self.codex_frame = ttk.LabelFrame(outer, text="Codex 账号模式", padding=16)
        self.codex_frame.grid(row=1, column=0, sticky="nsew")
        self.codex_frame.columnconfigure(0, weight=1)
        ttk.Label(
            self.codex_frame,
            text=(
                "完整四阶段由 EXE 内的程序调度器直接驱动 Codex SDK；每篇论文在各阶段固定一个会话，"
                "Step 顺序、并发、重试和交接都由程序状态机管理，不再创建 AI 调度会话。"
                "此登录使用独立配置目录，不覆盖 Codex Desktop 的账号。"
            ),
            foreground="#555",
            wraplength=820,
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 16))
        ttk.Label(
            self.codex_frame,
            textvariable=self.codex_account_status_var,
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))
        codex_actions = ttk.Frame(self.codex_frame)
        codex_actions.grid(row=2, column=0, sticky="w")
        self.codex_login_button = ttk.Button(
            codex_actions,
            text="连接 ChatGPT 账号",
            command=self._start_codex_browser_login,
        )
        self.codex_login_button.pack(side=tk.LEFT)
        self.codex_refresh_button = ttk.Button(
            codex_actions,
            text="刷新状态",
            command=self._refresh_codex_account,
        )
        self.codex_refresh_button.pack(side=tk.LEFT, padx=(8, 0))
        self.codex_logout_button = ttk.Button(
            codex_actions,
            text="断开账号",
            command=self._logout_codex_account,
        )
        self.codex_logout_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(codex_actions, text="关闭", command=self._close_window).pack(side=tk.LEFT, padx=(20, 0))
        self._interactive_widgets.extend(
            (self.codex_login_button, self.codex_refresh_button, self.codex_logout_button)
        )

        ttk.Label(outer, textvariable=self.status_var, anchor="w").grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _add_entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        key: str,
        *,
        show: str = "",
    ) -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        entry = ttk.Entry(parent, textvariable=self.profile_vars[key], show=show)
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self._interactive_widgets.append(entry)
        return row + 1

    def refresh(self, *, select_profile_id: str = "") -> None:
        if self.service is None:
            self.mode_var.set(API_KEY_MODE)
            self._update_mode_visibility()
            self._set_available(False)
            self.status_var.set("AI 配置存储模块尚不可用；当前未发起任务或网络请求。")
            return
        try:
            settings, profiles = self.service.snapshot()
        except Exception as exc:
            self._show_error("读取设置", exc)
            self._set_available(False)
            return

        self._profiles = profiles
        mode = str(settings.get("execution_mode") or LEGACY_CODEX_MODE).strip()
        self.mode_var.set(mode if mode in {API_KEY_MODE, LEGACY_CODEX_MODE} else LEGACY_CODEX_MODE)
        self._profile_items.clear()
        for item in self.profile_tree.get_children():
            self.profile_tree.delete(item)
        active_profile_id = str(settings.get("active_profile_id") or "").strip()
        for index, profile in enumerate(profiles):
            profile_id = str(profile.get("profile_id") or "").strip()
            item_id = f"profile-{index}"
            name = str(profile.get("display_name") or profile_id or "未命名配置")
            if profile_id and profile_id == active_profile_id:
                name += "（当前）"
            capability = str(profile.get("capability_status") or "untested").strip().casefold()
            self.profile_tree.insert("", tk.END, iid=item_id, text=name, values=(CAPABILITY_LABELS.get(capability, "状态异常"),))
            self._profile_items[item_id] = profile_id

        target = select_profile_id or self._editing_profile_id or active_profile_id
        selected_item = next(
            (item for item, profile_id in self._profile_items.items() if profile_id == target),
            "",
        )
        if not selected_item and self._profile_items:
            selected_item = next(iter(self._profile_items))
        if selected_item:
            self.profile_tree.selection_set(selected_item)
            self.profile_tree.focus(selected_item)
            self.profile_tree.see(selected_item)
            self._load_profile(self._profile_items[selected_item])
        else:
            self._new_profile()
        self._set_available(True)
        self._update_mode_visibility()
        self.status_var.set("设置已读取。")

    def _set_available(self, available: bool) -> None:
        for widget in self._interactive_widgets:
            try:
                if isinstance(widget, tk.Text):
                    widget.configure(state="normal" if available else "disabled")
                elif available:
                    widget.state(["!disabled"])
                else:
                    widget.state(["disabled"])
            except tk.TclError:
                continue

    def _update_request_url(self, *_args: Any) -> None:
        derived = responses_endpoint_url(self.profile_vars["base_url"].get())
        safe_url = _safe_request_url(derived)
        self.request_url_var.set(f"实际请求地址：{safe_url or '-'}")

    def _update_mode_visibility(self) -> None:
        if self.mode_var.get() == API_KEY_MODE:
            self.api_frame.grid()
            self.codex_frame.grid_remove()
        else:
            self.api_frame.grid_remove()
            self.codex_frame.grid()
            self.after_idle(self._refresh_codex_account)

    @staticmethod
    def _codex_account_status_text(status: Mapping[str, Any]) -> str:
        if status.get("authenticated") is True:
            plan = str(status.get("plan_type") or "unknown")
            return f"Codex 账号：已连接（{plan}）"
        if status.get("ok") is False:
            return "Codex 账号：状态检测失败"
        return "Codex 账号：尚未连接"

    def _set_codex_account_busy(self, busy: bool) -> None:
        self._codex_account_in_progress = busy
        state = "disabled" if busy else "normal"
        for widget in (
            self.codex_login_button,
            self.codex_refresh_button,
            self.codex_logout_button,
        ):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    def _refresh_codex_account(self) -> None:
        if self._codex_account_in_progress:
            return
        self._set_codex_account_busy(True)
        self.codex_account_status_var.set("Codex 账号：正在检测…")

        def work() -> None:
            verified_status = getattr(
                self.codex_account_service, "verified_status", None
            )
            status = (
                verified_status()
                if callable(verified_status)
                else self.codex_account_service.status()
            )

            def finish() -> None:
                self._set_codex_account_busy(False)
                self.codex_account_status_var.set(self._codex_account_status_text(status))

            try:
                self.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="codex-account-status", daemon=True).start()

    def _start_codex_browser_login(self) -> None:
        if self._codex_account_in_progress:
            return
        if not messagebox.askyesno(
            "连接 ChatGPT 账号",
            "将打开官方登录页面，为本程序的独立 Codex SDK 环境授权。\n\n"
            "不会覆盖 Codex Desktop 当前登录，也不会把登录地址、验证码或令牌写入日志。是否继续？",
            parent=self,
            default=messagebox.NO,
        ):
            return
        self._set_codex_account_busy(True)
        self.codex_account_status_var.set(
            "Codex 账号：等待浏览器授权并确认本地状态…"
        )

        def work() -> None:
            error_code = ""
            try:
                challenge = self.codex_account_service.begin_browser_login()
                if not webbrowser.open(challenge.verification_url, new=2):
                    self.codex_account_service.cancel_login(challenge.attempt_id)
                    raise RuntimeError("browser_open_failed")
                result = self.codex_account_service.wait_for_login(challenge.attempt_id)
                status = result.status
                success = bool(result.success and status.get("authenticated"))
                error_code = str(getattr(result, "error_code", "") or "")
            except BaseException as exc:
                status = {}
                success = False
                error_code = str(getattr(exc, "code", "") or type(exc).__name__)
            diagnostic = (
                error_code
                if _SAFE_DIAGNOSTIC_TOKEN.fullmatch(error_code)
                else "CODEX_LOGIN_NOT_COMPLETED"
            )

            def finish() -> None:
                self._set_codex_account_busy(False)
                if success:
                    self.codex_account_status_var.set(self._codex_account_status_text(status))
                    messagebox.showinfo(
                        "连接 ChatGPT 账号",
                        "Codex SDK 账号已连接，可以使用本地事件调度器。",
                        parent=self,
                    )
                else:
                    self.codex_account_status_var.set("Codex 账号：连接未完成")
                    messagebox.showerror(
                        "连接 ChatGPT 账号",
                        f"账号连接未完成（{diagnostic}）。未启动论文任务。",
                        parent=self,
                    )

            try:
                self.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="codex-account-login", daemon=True).start()

    def _logout_codex_account(self) -> None:
        if self._codex_account_in_progress:
            return
        if not messagebox.askyesno(
            "断开 Codex 账号",
            "只断开本程序独立 Codex SDK 环境中的账号，不影响 Codex Desktop。是否继续？",
            parent=self,
            default=messagebox.NO,
        ):
            return
        self._set_codex_account_busy(True)

        def work() -> None:
            try:
                status = self.codex_account_service.logout()
            except BaseException:
                status = {"ok": False, "authenticated": False}

            def finish() -> None:
                self._set_codex_account_busy(False)
                self.codex_account_status_var.set(self._codex_account_status_text(status))

            try:
                self.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="codex-account-logout", daemon=True).start()

    def _close_window(self) -> None:
        try:
            self.codex_account_service.close()
        except Exception:
            pass
        self.destroy()

    def _on_profile_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selection = self.profile_tree.selection()
        if not selection:
            return
        profile_id = self._profile_items.get(selection[0], "")
        if profile_id:
            self._load_profile(profile_id)

    def _load_profile(self, profile_id: str) -> None:
        profile = next(
            (item for item in self._profiles if str(item.get("profile_id") or "") == profile_id),
            None,
        )
        if profile is None:
            return
        self._editing_profile_id = profile_id
        self._editing_revision = profile.get("revision")
        for key in ("display_name", "base_url", "default_model", "reasoning_effort", "wire_api", "proxy_url"):
            self.profile_vars[key].set(str(profile.get(key) or ("responses" if key == "wire_api" else "")))
        self.profile_vars["api_key"].set("")
        headers = profile.get("custom_headers")
        self._set_custom_headers(headers if isinstance(headers, Mapping) else {})
        capability = str(profile.get("capability_status") or "untested").strip().casefold()
        tested_at = str(profile.get("last_tested_at") or "").strip()
        capability_text = CAPABILITY_LABELS.get(capability, "状态异常")
        self.capability_var.set(
            f"兼容状态：{capability_text}" + (f"；最近检测：{tested_at}" if tested_at else "")
        )
        if self.service is None:
            self.key_status_var.set("密钥：无法检测")
            return
        try:
            has_key = self.service.has_api_key(profile_id)
        except Exception as exc:
            self.key_status_var.set("密钥：检测失败")
            self._show_error("读取密钥状态", exc)
            return
        self.key_status_var.set(
            "密钥：已安全保存（输入新值可替换）"
            if has_key
            else "密钥：尚未保存"
        )

    def _set_custom_headers(self, value: Mapping[str, Any]) -> None:
        self.custom_headers_text.configure(state="normal")
        self.custom_headers_text.delete("1.0", tk.END)
        if value:
            self.custom_headers_text.insert("1.0", json.dumps(dict(value), ensure_ascii=False, indent=2))

    def _new_profile(self) -> None:
        selected = self.profile_tree.selection()
        if selected:
            self.profile_tree.selection_remove(*selected)
        self._editing_profile_id = ""
        self._editing_revision = None
        for key, var in self.profile_vars.items():
            var.set("responses" if key == "wire_api" else "")
        self._set_custom_headers({})
        self.key_status_var.set("密钥：尚未保存")
        self.capability_var.set("兼容状态：未检测")
        self.status_var.set("正在新增 API 配置。")

    def _form_values(self) -> dict[str, Any]:
        return {
            key: var.get()
            for key, var in self.profile_vars.items()
            if key != "api_key"
        } | {"custom_headers": _parse_custom_headers(self.custom_headers_text.get("1.0", "end"))}

    def _save_profile(self) -> None:
        if self.service is None:
            return
        key_value = self.profile_vars["api_key"].get()
        try:
            saved = self.service.save_profile(
                profile_id=self._editing_profile_id,
                expected_revision=self._editing_revision,
                values=self._form_values(),
                api_key=key_value,
            )
        except Exception as exc:
            self._show_error("保存 API 配置", exc)
            return
        finally:
            self.profile_vars["api_key"].set("")
        saved_id = str(saved.get("profile_id") or self._editing_profile_id)
        self.refresh(select_profile_id=saved_id)
        self.status_var.set("API 配置已保存；未启动任务。")

    def _delete_profile(self) -> None:
        if self.service is None or not self._editing_profile_id:
            messagebox.showinfo("删除 API 配置", "请先选择要删除的配置。", parent=self)
            return
        if not messagebox.askyesno(
            "删除 API 配置",
            "确定删除当前配置及其安全保存的 API Key 吗？",
            parent=self,
            default=messagebox.NO,
        ):
            return
        try:
            self.service.delete_profile(self._editing_profile_id, self._editing_revision)
        except Exception as exc:
            self._show_error("删除 API 配置", exc)
            return
        self._editing_profile_id = ""
        self._editing_revision = None
        self.refresh()
        self.status_var.set("API 配置已删除。")

    def _test_profile(self) -> None:
        if not self._editing_profile_id:
            messagebox.showinfo("测试连接", "请先保存并选择一个 API 配置。", parent=self)
            return
        if self.test_callback is None:
            messagebox.showinfo(
                "测试连接",
                "当前未配置连接检测，未发起网络请求。",
                parent=self,
            )
            return
        if self.service is None:
            return
        if self._test_in_progress:
            return
        request_url = _safe_request_url(
            responses_endpoint_url(self.profile_vars["base_url"].get())
        )
        request_line = f"\n实际请求地址：{request_url}\n" if request_url else "\n"
        if not messagebox.askyesno(
            "测试完整兼容性",
            "将通过内置 Codex SDK 发起一次最小、只读、无工具的真实模型请求。\n\n"
            f"{request_line}"
            "这会产生少量 API 用量；不会读取或修改论文文件，也不会自动重试。\n"
            "是否继续？",
            parent=self,
            default=messagebox.NO,
        ):
            return
        profile_id = self._editing_profile_id
        self._test_in_progress = True
        self.test_button.configure(state="disabled")
        self.status_var.set("正在进行完整 Codex/Responses 兼容性测试……")

        def finish_common() -> None:
            self._test_in_progress = False
            try:
                self.test_button.configure(state="normal")
            except tk.TclError:
                pass

        def finish_error(exc: BaseException) -> None:
            finish_common()
            self.refresh(select_profile_id=profile_id)
            self._show_error("连接检测", exc)

        def finish_success(result: Any) -> None:
            finish_common()
            self.refresh(select_profile_id=profile_id)
            result_record = _record_dict(result)
            capability = str(result_record.get("capability_status") or "").strip().casefold()
            result_text = CAPABILITY_LABELS.get(capability, "已完成")
            request_url = _safe_request_url(result_record.get("request_url"))
            request_line = f"\n实际请求地址：{request_url}" if request_url else ""
            self.status_var.set(f"完整兼容性测试完成：{result_text}。")
            messagebox.showinfo(
                "测试连接",
                f"连接检测结果：{result_text}。{request_line}\n\n"
                "已执行 1 次真实模型请求，未调用工具。",
                parent=self,
            )

        def work() -> None:
            try:
                result = self.test_callback(self.service.store, profile_id)
            except BaseException as exc:
                self.after(0, lambda error=exc: finish_error(error))
            else:
                self.after(0, lambda payload=result: finish_success(payload))

        threading.Thread(
            target=work,
            name="ai-provider-compatibility-test",
            daemon=True,
        ).start()

    def _save_mode(self) -> None:
        if self.service is None:
            return
        try:
            settings = self.service.save_mode(
                self.mode_var.get(),
                self._editing_profile_id,
                self._profiles,
            )
        except Exception as exc:
            # Active-task switch protection and all other concurrency rules are
            # enforced by the store; surface its safe reason in Chinese context.
            self._show_error("保存执行模式", exc)
            return
        mode = str(settings.get("execution_mode") or self.mode_var.get())
        self.mode_var.set(mode if mode in {API_KEY_MODE, LEGACY_CODEX_MODE} else self.mode_var.get())
        self._update_mode_visibility()
        self.status_var.set("执行模式已保存；未启动任务。")
        refresh_parent = getattr(self.master, "refresh", None)
        if callable(refresh_parent):
            self.after_idle(refresh_parent)

    def _toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_frame.grid()
            self.advanced_button.configure(text="收起高级字段")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text="显示高级字段")

    def _show_error(self, action: str, exc: BaseException) -> None:
        detail = _format_error_for_display(exc)
        messagebox.showerror("AI执行与API", f"{action}失败。\n\n{detail}", parent=self)
        self.status_var.set(f"{action}失败。")


def open_ai_execution_settings(
    parent: tk.Misc,
    *,
    store: Any | None = None,
    store_factory: Callable[[], Any] | None = None,
    test_callback: Callable[[Any, str], Any] | None = None,
    codex_account_service: Any | None = None,
) -> AIExecutionSettingsWindow:
    """Open or focus the single V1 AI execution settings window."""
    attribute = "_ai_execution_settings_window"
    existing = getattr(parent, attribute, None)
    if isinstance(existing, AIExecutionSettingsWindow):
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return existing
        except tk.TclError:
            pass
    window = AIExecutionSettingsWindow(
        parent,
        store=store,
        store_factory=store_factory,
        test_callback=test_callback,
        codex_account_service=codex_account_service,
    )
    setattr(parent, attribute, window)

    def clear_reference(event: tk.Event[tk.Misc]) -> None:
        if event.widget is window and getattr(parent, attribute, None) is window:
            setattr(parent, attribute, None)

    window.bind("<Destroy>", clear_reference, add="+")
    return window
