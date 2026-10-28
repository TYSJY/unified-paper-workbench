from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from ..account_semantics import canonical_account_scope, canonical_account_status


STATUS_LABELS = {
    "unknown": "未知",
    "active": "启用",
    "verified": "已验证",
    "logged_in": "已登录",
    "session_ready": "会话可用",
    "not_configured": "未配置",
    "login_failed": "登录失败",
    "invalid_password": "密码需核对",
    "imported_from_legacy": "从旧账号库导入",
    "imported_from_legacy_credential_store": "从旧凭据库导入",
    "configured_unverified": "已配置（待登录验证）",
    "configured": "已配置",
    "available": "可用",
    "adapter_planned": "适配计划中",
    "discovered": "已发现",
    "needs_discovery": "待补充",
    "ready": "就绪",
    "blocked": "已阻塞",
    "source_status_conflict": "来源状态不一致",
}

SCHEDULER_MODE_LABELS = {
    "manual": "手动暂停",
    "run_once": "运行一轮",
    "scheduled": "定时运行",
    "auto": "自动调度",
    "monitor_only": "仅监控",
}

AUTHOR_STATUS_LABELS = {
    "READY": "就绪",
    "DRAFT": "草稿",
}

PARALLEL_LIMIT_LABELS = {
    "all": "全部",
}

ACCOUNT_SCOPE_LABELS = {
    "email": "邮箱账号",
    "journal_system": "投稿系统账号",
    "external": "外部服务账号",
}

_ACCOUNT_SUMMARY_FIELDS = {
    "record_key",
    "account_id",
    "scope",
    "provider",
    "username",
    "login_url",
    "status",
    "last_verified_at",
    "password_saved",
    "password_storage",
    "source_kind",
    "pending_migration",
    "read_only",
    "auto_fill_ready",
}


def responsive_window_size(
    screen_width: int,
    screen_height: int,
    *,
    preferred: tuple[int, int],
    minimum: tuple[int, int],
    margins: tuple[int, int] = (60, 100),
) -> tuple[int, int, int, int]:
    """Fit a Tk client window inside the visible screen and return size/minsize."""

    width_limit = max(640, int(screen_width) - max(0, int(margins[0])))
    height_limit = max(420, int(screen_height) - max(0, int(margins[1])))
    minimum_width = min(max(640, int(minimum[0])), width_limit)
    minimum_height = min(max(420, int(minimum[1])), height_limit)
    width = max(minimum_width, min(int(preferred[0]), width_limit))
    height = max(minimum_height, min(int(preferred[1]), height_limit))
    return width, height, minimum_width, minimum_height


def _display_text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    return " ".join(_display_text(value).casefold().split())


def _origin(value: Any) -> str:
    text = _display_text(value)
    if not text:
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _stable_group_key(prefix: str, values: Iterable[Any]) -> str:
    payload = "\x1f".join(_normalized(value) for value in values)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _account_rows_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_scope = canonical_account_scope(left.get("scope"))
    right_scope = canonical_account_scope(right.get("scope"))
    if left_scope != right_scope:
        return False
    left_identity = _normalized(left.get("username"))
    right_identity = _normalized(right.get("username"))
    if not left_identity or left_identity != right_identity:
        return False
    left_provider = _normalized(left.get("provider"))
    right_provider = _normalized(right.get("provider"))
    same_provider = bool(left_provider and left_provider == right_provider)
    left_origin = _origin(left.get("login_url"))
    right_origin = _origin(right.get("login_url"))
    same_origin = bool(left_origin and left_origin == right_origin)
    return same_provider or same_origin


def group_account_summaries(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Build a value-free logical account view without mutating registry rows.

    A Smart compatibility row and a DPAPI row are folded together only when
    their canonical scope and username match and they also share a provider or
    login origin.  Ambiguous rows remain separate.  The underlying record keys
    stay available to the UI action layer but are never used as display text.
    """

    groups: list[dict[str, Any]] = []
    for raw_row in rows:
        row = {key: raw_row.get(key) for key in _ACCOUNT_SUMMARY_FIELDS if key in raw_row}
        matching = next(
            (
                group
                for group in groups
                if any(_account_rows_match(row, existing) for existing in group["records"])
            ),
            None,
        )
        if matching is None:
            matching = {"kind": "account", "records": []}
            groups.append(matching)
        matching["records"].append(row)

    finalized: list[dict[str, Any]] = []
    for group in groups:
        records = group["records"]
        representative = next(
            (row for row in records if _display_text(row.get("provider"))),
            records[0],
        )
        scope = canonical_account_scope(representative.get("scope"))
        provider = _display_text(representative.get("provider")) or "未命名网站或系统"
        username = _display_text(representative.get("username")) or "未填写账号"
        origins = sorted({_origin(row.get("login_url")) for row in records if _origin(row.get("login_url"))})
        sources = sorted({_display_text(row.get("source_kind")) for row in records if row.get("source_kind")})
        statuses = {
            canonical_account_status(row.get("status"))
            for row in records
        }
        display_status = next(iter(statuses), "unknown") if len(statuses) <= 1 else "source_status_conflict"
        searchable = " ".join(
            _display_text(value)
            for row in records
            for value in (
                row.get("account_id"),
                row.get("scope"),
                row.get("provider"),
                row.get("username"),
                row.get("login_url"),
                row.get("status"),
            )
        )
        finalized.append(
            {
                "kind": "account",
                "group_key": _stable_group_key(
                    "account",
                    (scope, username, _normalized(provider), origins[0] if origins else "", records[0].get("record_key")),
                ),
                "title": provider,
                "subtitle": f"{username} · {ACCOUNT_SCOPE_LABELS.get(scope, '未分类账号')}"
                + (f" · {len(records)} 条底层记录" if len(records) > 1 else ""),
                "scope": scope,
                "provider": provider,
                "username": username,
                "login_url": next(
                    (_display_text(row.get("login_url")) for row in records if row.get("login_url")),
                    "",
                ),
                "status": display_status,
                "sources": sources,
                "auto_fill_ready": all(bool(row.get("auto_fill_ready")) for row in records),
                "records": records,
                "search_text": searchable,
            }
        )
    return sorted(finalized, key=lambda item: (_normalized(item["title"]), _normalized(item["username"])))


def _site_rows_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_label = _normalized(left.get("label"))
    right_label = _normalized(right.get("label"))
    if left_label and left_label == right_label:
        return True
    left_origins = {_origin(left.get("login_url")), _origin(left.get("submission_url"))} - {""}
    right_origins = {_origin(right.get("login_url")), _origin(right.get("submission_url"))} - {""}
    return bool(left_origins & right_origins)


def group_submission_sites(
    systems: Iterable[Mapping[str, Any]],
    journals: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Group duplicate platform rows and attach journal counts for display only."""

    journal_rows = [dict(row) for row in journals]
    groups: list[dict[str, Any]] = []
    for raw_row in systems:
        row = dict(raw_row)
        matching = next(
            (group for group in groups if any(_site_rows_match(row, existing) for existing in group["records"])),
            None,
        )
        if matching is None:
            matching = {"kind": "site", "records": []}
            groups.append(matching)
        matching["records"].append(row)

    finalized: list[dict[str, Any]] = []
    for group in groups:
        records = group["records"]
        system_ids = {_normalized(row.get("system_id")) for row in records}
        linked_journals = [row for row in journal_rows if _normalized(row.get("system_id")) in system_ids]
        representative = next((row for row in records if row.get("label")), records[0])
        title = _display_text(representative.get("label")) or "未命名投稿站点"
        searchable = " ".join(
            _display_text(value)
            for row in [*records, *linked_journals]
            for value in row.values()
            if isinstance(value, (str, int, float))
        )
        finalized.append(
            {
                "kind": "site",
                "group_key": _stable_group_key(
                    "site",
                    (title, _origin(representative.get("login_url")), records[0].get("system_id")),
                ),
                "title": title,
                "subtitle": f"{len(linked_journals)} 本期刊"
                + (f" · {len(records)} 条底层记录" if len(records) > 1 else ""),
                "status": next(
                    (_display_text(row.get("status")) for row in records if row.get("status")),
                    "unknown",
                ),
                "login_url": next(
                    (_display_text(row.get("login_url")) for row in records if row.get("login_url")),
                    "",
                ),
                "submission_url": next(
                    (_display_text(row.get("submission_url")) for row in records if row.get("submission_url")),
                    "",
                ),
                "records": records,
                "journals": linked_journals,
                "search_text": searchable,
            }
        )
    return sorted(finalized, key=lambda item: _normalized(item["title"]))


def group_journal_records(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold exact same-journal display records while retaining every backing row."""

    by_signature: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        signature = (_normalized(row.get("journal_name")), _normalized(row.get("system_id")))
        if not any(signature):
            signature = (_normalized(row.get("journal_key")), "")
        by_signature.setdefault(signature, []).append(row)

    groups: list[dict[str, Any]] = []
    for signature, records in by_signature.items():
        representative = records[0]
        title = _display_text(representative.get("journal_name")) or "未命名期刊"
        system_id = _display_text(representative.get("system_id"))
        system = _display_text(representative.get("system_label"))
        if not system:
            system = "已关联投稿系统" if system_id else "未关联投稿系统"
        searchable = " ".join(
            _display_text(value)
            for row in records
            for value in row.values()
            if isinstance(value, (str, int, float))
        )
        groups.append(
            {
                "kind": "journal",
                "group_key": _stable_group_key("journal", (*signature, records[0].get("journal_key"))),
                "title": title,
                "subtitle": system + (f" · {len(records)} 条底层记录" if len(records) > 1 else ""),
                "system_id": system_id,
                "system_label": system,
                "status": _display_text(representative.get("status")) or "unknown",
                "submission_url": _display_text(representative.get("submission_url")),
                "records": records,
                "search_text": searchable,
            }
        )
    return sorted(groups, key=lambda item: _normalized(item["title"]))


def filter_logical_groups(groups: Iterable[Mapping[str, Any]], query: Any) -> list[dict[str, Any]]:
    needle = _normalized(query)
    return [dict(group) for group in groups if not needle or needle in _normalized(group.get("search_text"))]


def status_label(value: Any) -> str:
    text = str(value or "unknown").strip()
    normalized = canonical_account_status(text)
    if normalized in STATUS_LABELS:
        return STATUS_LABELS[normalized]
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return text if len(text) <= 18 else text[:17] + "…"
    return "需核对"


def scheduler_mode_label(value: Any) -> str:
    token = str(value or "").strip().lower()
    return SCHEDULER_MODE_LABELS.get(token, "未知" if not token else "需核对，详见审计")


def scheduler_mode_value(label: Any) -> str:
    text = str(label or "").strip()
    reverse = {display: value for value, display in SCHEDULER_MODE_LABELS.items()}
    return reverse.get(text, text.lower())


def author_status_label(value: Any) -> str:
    token = str(value or "DRAFT").strip().upper()
    return AUTHOR_STATUS_LABELS.get(token, "需核对")


def author_status_value(label: Any) -> str:
    text = str(label or "").strip()
    reverse = {display: value for value, display in AUTHOR_STATUS_LABELS.items()}
    return reverse.get(text, text.upper() or "DRAFT")


def parallel_limit_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    return PARALLEL_LIMIT_LABELS.get(text, text)


def parallel_limit_value(label: Any) -> str:
    text = str(label or "").strip()
    reverse = {display: value for value, display in PARALLEL_LIMIT_LABELS.items()}
    return reverse.get(text, text.lower())
