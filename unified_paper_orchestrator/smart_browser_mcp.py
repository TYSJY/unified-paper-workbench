"""Project-owned MCP server exposing capability-scoped Smart browser tools."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .browser_session import LocalCdpProbeTransport, normalize_origin
from .io_utils import FileLock, now_iso, read_json, write_json_atomic
from .smart_browser_cdp import (
    SmartBrowserCdpController,
    SmartBrowserCdpError,
    is_final_submit_control,
)


SMART_BROWSER_CONTEXT_SCHEMA = "smart-submission-sdk-browser-context/v1"
SMART_BROWSER_CONTEXT_STORE_SCHEMA = "smart-submission-sdk-browser-context-store/v1"
SMART_BROWSER_CONTEXT_TTL = timedelta(hours=4)
SMART_BROWSER_MCP_SERVER_NAME = "unified-smart-browser"
SMART_BROWSER_MCP_PROTOCOL_VERSION = "2025-06-18"
SMART_BROWSER_CONTEXT_ID_PATTERN = re.compile(r"sbx_[0-9a-f]{48}")

SMART_BROWSER_TOOL_NAMES = (
    "browser_pages",
    "browser_files",
    "browser_snapshot",
    "browser_mail_search",
    "browser_navigate",
    "browser_wait",
    "browser_fill",
    "browser_select",
    "browser_upload",
    "browser_click",
    "browser_final_submit_once",
)


class _PageBoundConfirmationTransport:
    """Limit post-submit evidence capture to the page that was clicked.

    Multiple papers may have live review tabs on the same journal origin.
    Origin-only confirmation scanning could therefore attach another paper's
    confirmation page to the current authorization.  The final-submit tool
    already owns one exact page, so confirmation must stay on that target.
    """

    def __init__(self, page_id: str, delegate: Any | None = None) -> None:
        self.page_id = str(page_id or "").strip()
        self.delegate = delegate or LocalCdpProbeTransport()

    def list_targets(self, port: int) -> list[dict[str, Any]]:
        targets = self.delegate.list_targets(port)
        return [
            dict(item)
            for item in targets
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip() == self.page_id
        ]

    def evaluate_submission_confirmation_state(
        self,
        port: int,
        websocket_url: str,
    ) -> dict[str, Any]:
        return self.delegate.evaluate_submission_confirmation_state(
            port,
            websocket_url,
        )


class SmartBrowserContextError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.safe_message = str(message)
        super().__init__(self.safe_message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.safe_message,
        }


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


class SmartBrowserContextStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": SMART_BROWSER_CONTEXT_STORE_SCHEMA,
            "contexts": {},
            "page_claims": {},
            "updated_at": now_iso(),
        }

    def _load(self) -> dict[str, Any]:
        state = read_json(self.path, self._empty())
        if not isinstance(state, dict):
            state = self._empty()
        if state.get("schema") != SMART_BROWSER_CONTEXT_STORE_SCHEMA:
            state = self._empty()
        if not isinstance(state.get("contexts"), dict):
            state["contexts"] = {}
        if not isinstance(state.get("page_claims"), dict):
            state["page_claims"] = {}
        return state

    @staticmethod
    def _prune_page_claims(state: dict[str, Any]) -> dict[str, Any]:
        claims = state.setdefault("page_claims", {})
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for page_id, claim in list(claims.items()):
            claimed_at = _parse_time(
                claim.get("updated_at") if isinstance(claim, dict) else None
            )
            if (
                not isinstance(claim, dict)
                or not str(page_id).strip()
                or not str(claim.get("paper_id") or "").strip()
                or claimed_at is None
                or claimed_at < cutoff
            ):
                claims.pop(page_id, None)
        return claims

    @staticmethod
    def _page_records(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            page = dict(value)
            page_id = str(page.get("page_id") or "").strip()
            if not page_id or page_id in seen:
                continue
            seen.add(page_id)
            page["page_id"] = page_id
            page["origin"] = normalize_origin(
                page.get("origin") or page.get("url")
            )
            pages.append(page)
        return pages

    @staticmethod
    def _claim_matches(
        claim: Mapping[str, Any] | None,
        context: Mapping[str, Any],
    ) -> bool:
        return bool(
            isinstance(claim, Mapping)
            and str(claim.get("paper_id") or "") == str(context.get("paper_id") or "")
        )

    @staticmethod
    def _claim_payload(
        context: Mapping[str, Any],
        page: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "paper_id": str(context.get("paper_id") or ""),
            "context_id": str(context.get("context_id") or ""),
            "route_revision": int(context.get("route_revision") or 0),
            "attempt_id": str(context.get("attempt_id") or ""),
            "origin": normalize_origin(page.get("origin") or page.get("url")),
            "url": str(page.get("url") or "")[:2000],
            "updated_at": now_iso(),
        }

    def pages_for_context(
        self,
        context: Mapping[str, Any],
        values: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Allocate one browser tab per paper and expose only tabs owned by it."""

        pages = self._page_records(values)
        if not pages:
            return []
        target_origin = normalize_origin(context.get("target_origin"))
        with FileLock(self.lock_path):
            state = self._load()
            claims = self._prune_page_claims(state)
            visible_ids = {str(page["page_id"]) for page in pages}
            owned = [
                page
                for page in pages
                if self._claim_matches(claims.get(str(page["page_id"])), context)
            ]
            if not owned:
                preferred_id = str(
                    (
                        context.get("metadata")
                        if isinstance(context.get("metadata"), Mapping)
                        else {}
                    ).get("initial_page_id")
                    or ""
                ).strip()
                candidates = [
                    page
                    for page in pages
                    if str(page["page_id"]) == preferred_id
                    and not claims.get(str(page["page_id"]))
                ]
                if not candidates:
                    candidates = [
                        page
                        for page in pages
                        if normalize_origin(page.get("origin")) == target_origin
                        and not claims.get(str(page["page_id"]))
                    ]
                if not candidates:
                    candidates = [
                        page
                        for page in pages
                        if not claims.get(str(page["page_id"]))
                    ]
                if candidates:
                    selected = candidates[0]
                    claims[str(selected["page_id"])] = self._claim_payload(
                        context,
                        selected,
                    )
                    owned = [selected]
            for page in owned:
                page_id = str(page["page_id"])
                if page_id not in visible_ids:
                    continue
                claims[page_id] = self._claim_payload(context, page)
            state_context = state.get("contexts", {}).get(
                str(context.get("context_id") or "")
            )
            if isinstance(state_context, dict) and owned:
                metadata = state_context.setdefault("metadata", {})
                if isinstance(metadata, dict):
                    metadata["initial_page_id"] = str(owned[0]["page_id"])
            state["updated_at"] = now_iso()
            write_json_atomic(self.path, state)
        return owned

    def require_owned_page(
        self,
        context: Mapping[str, Any],
        page: Mapping[str, Any],
    ) -> str:
        page_id = str(page.get("page_id") or "").strip()
        if not page_id:
            raise SmartBrowserContextError(
                "SMART_BROWSER_PAGE_REQUIRED",
                "this paper does not have an allocated browser page",
            )
        with FileLock(self.lock_path):
            state = self._load()
            claims = self._prune_page_claims(state)
            claim = claims.get(page_id)
            if not self._claim_matches(claim, context):
                raise SmartBrowserContextError(
                    "SMART_BROWSER_PAGE_OWNED_BY_OTHER_PAPER",
                    "the requested browser page belongs to a different paper",
                )
            claims[page_id] = self._claim_payload(context, page)
            state["updated_at"] = now_iso()
            write_json_atomic(self.path, state)
        return page_id

    def claim_transition_pages(
        self,
        context: Mapping[str, Any],
        *,
        source_page_id: str,
        before: Iterable[Mapping[str, Any]],
        after: Iterable[Mapping[str, Any]],
    ) -> None:
        """Keep navigated tabs and newly opened tabs bound to the initiating paper."""

        before_pages = {
            str(page["page_id"]): page
            for page in self._page_records(before)
        }
        after_pages = self._page_records(after)
        with FileLock(self.lock_path):
            state = self._load()
            claims = self._prune_page_claims(state)
            for page in after_pages:
                page_id = str(page["page_id"])
                old = before_pages.get(page_id)
                changed = bool(
                    old
                    and (
                        normalize_origin(old.get("origin") or old.get("url"))
                        != normalize_origin(page.get("origin") or page.get("url"))
                        or str(old.get("url") or "") != str(page.get("url") or "")
                    )
                )
                should_claim = (
                    page_id == str(source_page_id or "")
                    or page_id not in before_pages
                    or changed
                )
                if not should_claim:
                    continue
                existing = claims.get(page_id)
                if existing and not self._claim_matches(existing, context):
                    continue
                claims[page_id] = self._claim_payload(context, page)
            state["updated_at"] = now_iso()
            write_json_atomic(self.path, state)

    @staticmethod
    def _normalize_origins(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            origin = normalize_origin(value)
            if origin and origin not in result:
                result.append(origin)
        if not result:
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_ORIGIN_REQUIRED",
                "at least one verified browser origin is required",
            )
        return result

    @staticmethod
    def _normalize_files(
        values: Iterable[Path | str],
        roots: Iterable[Path | str],
    ) -> tuple[list[str], list[str]]:
        clean_roots: list[Path] = []
        for value in roots:
            root = Path(value).expanduser().resolve(strict=False)
            if root not in clean_roots:
                clean_roots.append(root)
        clean_files: list[str] = []
        for value in values:
            path = Path(value).expanduser().resolve(strict=False)
            if (
                not path.is_file()
                or path.is_symlink()
                or (clean_roots and not any(_inside(path, root) for root in clean_roots))
            ):
                continue
            text = str(path)
            if text not in clean_files:
                clean_files.append(text)
        return clean_files, [str(item) for item in clean_roots]

    def create(
        self,
        *,
        paper_id: str,
        route_revision: int,
        attempt_id: str,
        thread_id: str,
        allowed_origins: Iterable[str],
        allowed_files: Iterable[Path | str] = (),
        allowed_file_roots: Iterable[Path | str] = (),
        target_origin: str = "",
        ttl: timedelta = SMART_BROWSER_CONTEXT_TTL,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_paper_id = str(paper_id or "").strip()
        clean_attempt_id = str(attempt_id or "").strip()
        clean_thread_id = str(thread_id or "").strip()
        if not clean_paper_id or not clean_attempt_id or not clean_thread_id:
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_BINDING_REQUIRED",
                "paper, attempt, and fixed SDK thread bindings are required",
            )
        origins = self._normalize_origins(allowed_origins)
        target = normalize_origin(target_origin) or origins[0]
        if target not in origins:
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_TARGET_DENIED",
                "the target origin is not present in the verified origin allow-list",
            )
        files, roots = self._normalize_files(allowed_files, allowed_file_roots)
        now = datetime.now(timezone.utc)
        expires = now + min(max(ttl, timedelta(minutes=5)), timedelta(hours=8))
        context_id = "sbx_" + os.urandom(24).hex()
        context = {
            "schema": SMART_BROWSER_CONTEXT_SCHEMA,
            "context_id": context_id,
            "paper_id": clean_paper_id,
            "route_revision": max(0, int(route_revision)),
            "attempt_id": clean_attempt_id,
            "thread_id": clean_thread_id,
            "target_origin": target,
            "allowed_origins": origins,
            "allowed_files": files,
            "allowed_file_roots": roots,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "retired_at": "",
            "metadata": {
                str(key): value
                for key, value in dict(metadata or {}).items()
                if str(key) in {"submission_system_id", "target_journal", "authorization_id"}
            },
        }
        with FileLock(self.lock_path):
            state = self._load()
            contexts = state.setdefault("contexts", {})
            current = datetime.now(timezone.utc)
            for old_id, old in list(contexts.items()):
                if not isinstance(old, dict):
                    contexts.pop(old_id, None)
                    continue
                expiry = _parse_time(old.get("expires_at"))
                if expiry is None or expiry < current - timedelta(hours=24):
                    contexts.pop(old_id, None)
            contexts[context_id] = context
            state["updated_at"] = now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, state)
        return copy.deepcopy(context)

    def load_active(self, context_id: str) -> dict[str, Any]:
        clean_id = str(context_id or "").strip()
        if not SMART_BROWSER_CONTEXT_ID_PATTERN.fullmatch(clean_id):
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_INVALID",
                "the SDK browser capability id is invalid",
            )
        with FileLock(self.lock_path):
            state = self._load()
            context = state.get("contexts", {}).get(clean_id)
        if not isinstance(context, dict) or context.get("schema") != SMART_BROWSER_CONTEXT_SCHEMA:
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_MISSING",
                "the SDK browser capability is missing or no longer active",
            )
        expires = _parse_time(context.get("expires_at"))
        if (
            str(context.get("retired_at") or "")
            or expires is None
            or datetime.now(timezone.utc) >= expires
        ):
            raise SmartBrowserContextError(
                "SMART_BROWSER_CONTEXT_EXPIRED",
                "the SDK browser capability has expired; start a fresh paper turn",
            )
        return copy.deepcopy(context)

    def retire(self, context_id: str) -> None:
        clean_id = str(context_id or "").strip()
        if not SMART_BROWSER_CONTEXT_ID_PATTERN.fullmatch(clean_id):
            return
        with FileLock(self.lock_path):
            state = self._load()
            context = state.get("contexts", {}).get(clean_id)
            if not isinstance(context, dict) or str(context.get("retired_at") or ""):
                return
            context["retired_at"] = now_iso()
            state["updated_at"] = now_iso()
            write_json_atomic(self.path, state)


def _required_string(arguments: Mapping[str, Any], key: str) -> str:
    value = str(arguments.get(key) or "").strip()
    if not value:
        raise SmartBrowserContextError(
            "SMART_BROWSER_TOOL_ARGUMENT_REQUIRED",
            f"browser tool argument {key!r} is required",
        )
    return value


class SmartBrowserMcpTools:
    def __init__(
        self,
        context_store: SmartBrowserContextStore,
        *,
        controller_factory: Callable[[], SmartBrowserCdpController] = SmartBrowserCdpController,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.context_store = context_store
        self.controller_factory = controller_factory
        self.sleep = sleep

    def _context(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.context_store.load_active(
            _required_string(arguments, "context_id")
        )

    @staticmethod
    def _page_id(arguments: Mapping[str, Any]) -> str:
        return str(arguments.get("page_id") or "").strip()

    @staticmethod
    def _allowed_files(
        context: Mapping[str, Any],
        requested: Iterable[str],
    ) -> list[str]:
        approved: dict[str, str] = {}
        for index, value in enumerate(context.get("allowed_files", []), start=1):
            if not str(value).strip():
                continue
            resolved = str(Path(value).expanduser().resolve(strict=False))
            approved[resolved.casefold()] = resolved
            approved[f"file-{index:04d}"] = resolved
        result: list[str] = []
        for value in requested:
            clean = str(value or "").strip()
            canonical = approved.get(clean.casefold())
            if canonical is None:
                resolved = str(Path(clean).expanduser().resolve(strict=False))
                canonical = approved.get(resolved.casefold())
            if canonical is None:
                raise SmartBrowserContextError(
                    "SMART_BROWSER_UPLOAD_FILE_DENIED",
                    "the requested upload file is not in this paper turn's immutable allow-list",
                )
            if canonical not in result:
                result.append(canonical)
        if not result:
            raise SmartBrowserContextError(
                "SMART_BROWSER_UPLOAD_FILE_REQUIRED",
                "at least one approved upload file is required",
            )
        return result

    def _owned_pages(
        self,
        context: Mapping[str, Any],
        controller: SmartBrowserCdpController,
    ) -> list[dict[str, Any]]:
        return self.context_store.pages_for_context(
            context,
            controller.list_pages(context["allowed_origins"]),
        )

    def _owned_page_id(
        self,
        context: Mapping[str, Any],
        controller: SmartBrowserCdpController,
        requested_page_id: str,
    ) -> str:
        pages = self._owned_pages(context, controller)
        if requested_page_id:
            matches = [
                page
                for page in pages
                if str(page.get("page_id") or "") == requested_page_id
            ]
            if not matches:
                raise SmartBrowserContextError(
                    "SMART_BROWSER_PAGE_OWNED_BY_OTHER_PAPER",
                    "the requested browser page is not allocated to this paper",
                )
            return self.context_store.require_owned_page(context, matches[0])
        if not pages:
            raise SmartBrowserContextError(
                "SMART_BROWSER_PAGE_REQUIRED",
                "no unallocated browser page is available for this paper",
            )
        return self.context_store.require_owned_page(context, pages[0])

    def _record_page_transition(
        self,
        context: Mapping[str, Any],
        controller: SmartBrowserCdpController,
        *,
        source_page_id: str,
        before: Iterable[Mapping[str, Any]],
    ) -> None:
        after = controller.list_pages(context["allowed_origins"])
        self.context_store.claim_transition_pages(
            context,
            source_page_id=source_page_id,
            before=before,
            after=after,
        )

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        context = self._context(arguments)
        controller = self.controller_factory()
        allowed_origins = context["allowed_origins"]
        page_id = self._page_id(arguments)
        if name == "browser_pages":
            return {
                "ok": True,
                "context_id": context["context_id"],
                "paper_id": context["paper_id"],
                "pages": self._owned_pages(context, controller),
            }
        if name == "browser_files":
            files: list[dict[str, Any]] = []
            for index, value in enumerate(context.get("allowed_files", []), start=1):
                path = Path(str(value))
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                files.append(
                    {
                        "file_id": f"file-{index:04d}",
                        "name": path.name,
                        "suffix": path.suffix.lower(),
                        "size_bytes": int(size),
                    }
                )
            return {
                "ok": True,
                "context_id": context["context_id"],
                "paper_id": context["paper_id"],
                "files": files,
            }
        if name == "browser_snapshot":
            page_id = self._owned_page_id(context, controller, page_id)
            return controller.snapshot(
                allowed_origins,
                page_id=page_id,
                max_text_chars=int(arguments.get("max_text_chars") or 16_000),
                max_controls=int(arguments.get("max_controls") or 140),
            )
        if name == "browser_mail_search":
            page_id = self._owned_page_id(context, controller, page_id)
            target_origin = normalize_origin(context.get("target_origin"))
            allowed = {normalize_origin(item) for item in allowed_origins}
            if (
                target_origin != "https://mail.google.com:443"
                or allowed != {"https://mail.google.com:443"}
            ):
                raise SmartBrowserContextError(
                    "SMART_BROWSER_MAIL_CONTEXT_REQUIRED",
                    "mail search requires an exact Gmail-only read capability",
                )
            query = _required_string(arguments, "query")
            if len(query) > 2000 or any(ord(char) < 32 for char in query):
                raise SmartBrowserContextError(
                    "SMART_BROWSER_MAIL_QUERY_INVALID",
                    "mail search query is empty, too long, or contains control characters",
                )
            destination = (
                "https://mail.google.com/mail/u/0/#search/"
                + urllib.parse.quote(query, safe="")
            )
            controller.navigate(
                allowed_origins,
                destination,
                page_id=page_id,
                wait_seconds=12.0,
            )
            self.sleep(1.5)
            result = controller.snapshot(
                allowed_origins,
                page_id=page_id,
                max_text_chars=int(arguments.get("max_text_chars") or 24_000),
                max_controls=int(arguments.get("max_controls") or 180),
            )
            result["mail_query"] = query
            result["mailbox_mutation_allowed"] = False
            return result
        if name == "browser_navigate":
            page_id = self._owned_page_id(context, controller, page_id)
            before = controller.list_pages(allowed_origins)
            result = controller.navigate(
                allowed_origins,
                _required_string(arguments, "url"),
                page_id=page_id,
            )
            self._record_page_transition(
                context,
                controller,
                source_page_id=page_id,
                before=before,
            )
            return result
        if name == "browser_wait":
            page_id = self._owned_page_id(context, controller, page_id)
            timeout_seconds = min(
                30.0,
                max(0.25, float(arguments.get("timeout_seconds") or 10.0)),
            )
            self.sleep(timeout_seconds)
            return controller.wait_ready(
                allowed_origins,
                page_id=page_id,
                timeout_seconds=timeout_seconds,
            )
        if name == "browser_fill":
            page_id = self._owned_page_id(context, controller, page_id)
            return controller.fill(
                allowed_origins,
                _required_string(arguments, "ref"),
                str(arguments.get("value") or ""),
                page_id=page_id,
            )
        if name == "browser_select":
            page_id = self._owned_page_id(context, controller, page_id)
            return controller.select(
                allowed_origins,
                _required_string(arguments, "ref"),
                _required_string(arguments, "option"),
                page_id=page_id,
            )
        if name == "browser_upload":
            page_id = self._owned_page_id(context, controller, page_id)
            raw_files = arguments.get("files")
            values = (
                [str(item) for item in raw_files]
                if isinstance(raw_files, list)
                else [_required_string(arguments, "file")]
            )
            return controller.upload(
                allowed_origins,
                _required_string(arguments, "ref"),
                self._allowed_files(context, values),
                page_id=page_id,
            )
        if name == "browser_click":
            page_id = self._owned_page_id(context, controller, page_id)
            before = controller.list_pages(allowed_origins)
            result = controller.click(
                allowed_origins,
                _required_string(arguments, "ref"),
                page_id=page_id,
                allow_final_submit=False,
            )
            self._record_page_transition(
                context,
                controller,
                source_page_id=page_id,
                before=before,
            )
            return result
        if name == "browser_final_submit_once":
            page_id = self._owned_page_id(context, controller, page_id)
            return self._final_submit_once(
                context,
                controller,
                ref=_required_string(arguments, "ref"),
                page_id=page_id,
                authorization_id=_required_string(arguments, "authorization_id"),
            )
        raise SmartBrowserContextError(
            "SMART_BROWSER_TOOL_UNKNOWN",
            f"unknown Smart browser tool: {name}",
        )

    def _final_submit_once(
        self,
        context: Mapping[str, Any],
        controller: SmartBrowserCdpController,
        *,
        ref: str,
        page_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        from .smart_submission import (
            capture_live_submission_confirmation,
            claim_final_submit_action,
            record_final_submit_result_unknown,
            record_visible_worker_result,
        )

        allowed_origins = context["allowed_origins"]
        control = controller.inspect_control(
            allowed_origins,
            ref,
            page_id=page_id,
        )
        control_semantics = str(
            control.get("label") or control.get("marker") or ""
        ).strip()
        if not is_final_submit_control(control_semantics):
            raise SmartBrowserContextError(
                "SMART_BROWSER_FINAL_CONTROL_NOT_VERIFIED",
                "the selected control is not recognizably the final submit action",
            )
        paper_id = str(context["paper_id"])
        route_revision = int(context["route_revision"])
        attempt_id = str(context["attempt_id"])
        thread_id = str(context["thread_id"])
        action_claimed = False

        def claim_immediately_before_click() -> None:
            nonlocal action_claimed
            claim_final_submit_action(
                paper_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
                thread_id=thread_id,
                authorization_id=authorization_id,
            )
            action_claimed = True

        try:
            click_result = controller.click(
                allowed_origins,
                ref,
                page_id=str(control.get("page_id") or page_id),
                allow_final_submit=True,
                before_click=claim_immediately_before_click,
            )
        except Exception as exc:
            if action_claimed:
                record_final_submit_result_unknown(
                    paper_id,
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                    thread_id=thread_id,
                    authorization_id=authorization_id,
                    detail=(
                        "The one-use final action was claimed, but the CDP click result "
                        f"became uncertain: {type(exc).__name__}."
                    ),
                )
            raise

        captured: dict[str, Any] | None = None
        last_error = ""
        clicked_page_id = str(click_result.get("page_id") or page_id).strip()
        confirmation_transport = _PageBoundConfirmationTransport(clicked_page_id)
        # The MCP call itself has a 55-second hard timeout.  One exact-page
        # probe is bounded by the CDP timeout, so eight attempts leave enough
        # time to record either positive evidence or a durable unknown result.
        for _index in range(8):
            try:
                captured = capture_live_submission_confirmation(
                    paper_id,
                    route_revision=route_revision,
                    attempt_id=attempt_id,
                    thread_id=thread_id,
                    authorization_id=authorization_id,
                    transport=confirmation_transport,
                )
                break
            except Exception as exc:
                last_error = type(exc).__name__
                self.sleep(0.5)
        if captured is None:
            unknown = record_final_submit_result_unknown(
                paper_id,
                route_revision=route_revision,
                attempt_id=attempt_id,
                thread_id=thread_id,
                authorization_id=authorization_id,
                detail=(
                    "The final control was activated once, but no trusted positive "
                    f"confirmation appeared inside the bounded capture window ({last_error or 'no marker'})."
                ),
            )
            return {
                "ok": False,
                "code": "SMART_FINAL_SUBMIT_RESULT_UNKNOWN",
                "clicked_once": True,
                "must_not_retry": True,
                "click": click_result,
                "unknown": {
                    key: value
                    for key, value in unknown.items()
                    if key != "state"
                },
            }
        recorded = record_visible_worker_result(
            paper_id,
            route_revision=route_revision,
            attempt_id=attempt_id,
            status="submitted",
            next_action="monitor_submission_email_and_dashboard",
            detail="Trusted post-submit confirmation captured by the SDK browser tool.",
            thread_id=thread_id,
            source="codex_sdk_browser_final_submit",
            submission_id=str(captured.get("manuscript_id") or ""),
            evidence_path=str(captured.get("evidence_path") or ""),
            confirmation_url=str(captured.get("confirmation_url") or ""),
            activate=False,
        )
        return {
            "ok": True,
            "code": "SMART_FINAL_SUBMIT_CONFIRMED",
            "clicked_once": True,
            "must_not_retry": True,
            "click": click_result,
            "confirmation": {
                key: captured.get(key)
                for key in (
                    "manuscript_id",
                    "confirmation_url",
                    "evidence_path",
                    "confirmation_marker",
                )
            },
            "recorded_status": recorded.get("status"),
        }


def _tool_schema(
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: Iterable[str],
    *,
    read_only: bool,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": "Immutable capability id supplied in the current paper prompt.",
                },
                **dict(properties),
            },
            "required": ["context_id", *list(required)],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": bool(read_only),
            "destructiveHint": name == "browser_final_submit_once",
            "idempotentHint": read_only,
            "openWorldHint": True,
        },
    }


def smart_browser_tool_specs() -> list[dict[str, Any]]:
    page_id = {
        "page_id": {
            "type": "string",
            "description": "Optional page id from browser_pages/browser_snapshot.",
        }
    }
    ref = {
        **page_id,
        "ref": {
            "type": "string",
            "description": "Control reference from the latest browser_snapshot.",
        },
    }
    return [
        _tool_schema(
            "browser_pages",
            "List only the open pages whose exact origins are verified for this paper.",
            {},
            (),
            read_only=True,
        ),
        _tool_schema(
            "browser_files",
            "List immutable upload file IDs, names, and sizes approved for this paper turn.",
            {},
            (),
            read_only=True,
        ),
        _tool_schema(
            "browser_snapshot",
            "Read bounded visible text and interactive controls. Password/code values, cookies, and storage are never returned.",
            {
                **page_id,
                "max_text_chars": {"type": "integer", "minimum": 1000, "maximum": 24000},
                "max_controls": {"type": "integer", "minimum": 10, "maximum": 180},
            },
            (),
            read_only=True,
        ),
        _tool_schema(
            "browser_mail_search",
            "Run one bounded Gmail search by exact query and return a snapshot of result subjects/snippets. This cannot send, delete, label, mark, or open mail.",
            {
                **page_id,
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "max_text_chars": {"type": "integer", "minimum": 1000, "maximum": 24000},
                "max_controls": {"type": "integer", "minimum": 10, "maximum": 180},
            },
            ("query",),
            read_only=True,
        ),
        _tool_schema(
            "browser_navigate",
            "Navigate an existing paper page to a URL on the verified origin allow-list.",
            {
                **page_id,
                "url": {"type": "string"},
            },
            ("url",),
            read_only=False,
        ),
        _tool_schema(
            "browser_wait",
            "Wait for a paper page to become interactive or complete.",
            {
                **page_id,
                "timeout_seconds": {"type": "number", "minimum": 0.25, "maximum": 30},
            },
            (),
            read_only=True,
        ),
        _tool_schema(
            "browser_fill",
            "Fill a normal text field. Password, OTP, payment, token, hidden, and file fields are denied.",
            {
                **ref,
                "value": {"type": "string"},
            },
            ("ref", "value"),
            read_only=False,
        ),
        _tool_schema(
            "browser_select",
            "Select one option by exact value or visible option label.",
            {
                **ref,
                "option": {"type": "string"},
            },
            ("ref", "option"),
            read_only=False,
        ),
        _tool_schema(
            "browser_upload",
            "Attach one or more file IDs returned by browser_files.",
            {
                **ref,
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
            },
            ("ref", "files"),
            read_only=False,
        ),
        _tool_schema(
            "browser_click",
            "Activate a normal page control. Any final-submit-like control is denied.",
            ref,
            ("ref",),
            read_only=False,
        ),
        _tool_schema(
            "browser_final_submit_once",
            "Claim the EXE-issued exact-attempt authorization, activate one verified final-submit control once, and capture trusted confirmation. Never retry this tool.",
            {
                **ref,
                "authorization_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            },
            ("ref", "authorization_id"),
            read_only=False,
        ),
    ]


class SmartBrowserMcpServer:
    """Minimal MCP stdio implementation; stdout is reserved for JSON-RPC."""

    def __init__(self, tools: SmartBrowserMcpTools) -> None:
        self.tools = tools

    @staticmethod
    def _result(message_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": dict(result),
        }

    @staticmethod
    def _error(
        message_id: Any,
        code: int,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {
                "code": int(code),
                "message": str(message),
            },
        }
        if data:
            payload["error"]["data"] = dict(data)
        return payload

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        message_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        if not method:
            return self._error(message_id, -32600, "Invalid Request")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            supported = {
                "2024-11-05",
                "2025-03-26",
                "2025-06-18",
                "2025-11-25",
            }
            protocol = (
                requested
                if requested in supported
                else SMART_BROWSER_MCP_PROTOCOL_VERSION
            )
            return self._result(
                message_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": SMART_BROWSER_MCP_SERVER_NAME,
                        "version": "1.0.0",
                    },
                    "instructions": (
                        "Use only the capability id in the current Smart Submission "
                        "paper prompt. Take a fresh snapshot before using control refs. "
                        "Never retry browser_final_submit_once."
                    ),
                },
            )
        if method == "ping":
            return self._result(message_id, {})
        if method == "tools/list":
            return self._result(message_id, {"tools": smart_browser_tool_specs()})
        if method == "resources/list":
            return self._result(message_id, {"resources": []})
        if method == "prompts/list":
            return self._result(message_id, {"prompts": []})
        if method == "logging/setLevel":
            return self._result(message_id, {})
        if method != "tools/call":
            return self._error(message_id, -32601, "Method not found")
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        try:
            result = self.tools.call(name, arguments)
            text = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return self._result(
                message_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": result,
                    "isError": not bool(result.get("ok", True)),
                },
            )
        except (SmartBrowserContextError, SmartBrowserCdpError) as exc:
            result = exc.as_dict()
        except Exception as exc:
            result = {
                "ok": False,
                "code": "SMART_BROWSER_TOOL_FAILED",
                "message": f"browser tool failed: {type(exc).__name__}",
            }
        text = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return self._result(
            message_id,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": result,
                "isError": True,
            },
        )

    def serve(self) -> int:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                response = self._error(None, -32700, "Parse error")
                print(
                    json.dumps(response, ensure_ascii=True, separators=(",", ":")),
                    flush=True,
                )
                continue
            messages = payload if isinstance(payload, list) else [payload]
            responses: list[dict[str, Any]] = []
            for message in messages:
                if not isinstance(message, Mapping):
                    responses.append(self._error(None, -32600, "Invalid Request"))
                    continue
                response = self.handle(message)
                if response is not None:
                    responses.append(response)
            if not responses:
                continue
            output: Any = responses if isinstance(payload, list) else responses[0]
            print(
                json.dumps(output, ensure_ascii=True, separators=(",", ":")),
                flush=True,
            )
        return 0


def serve_smart_browser_mcp(context_store_path: Path | str) -> int:
    store = SmartBrowserContextStore(context_store_path)
    server = SmartBrowserMcpServer(SmartBrowserMcpTools(store))
    return server.serve()


__all__ = [
    "SMART_BROWSER_CONTEXT_SCHEMA",
    "SMART_BROWSER_CONTEXT_STORE_SCHEMA",
    "SMART_BROWSER_MCP_SERVER_NAME",
    "SMART_BROWSER_TOOL_NAMES",
    "SmartBrowserContextError",
    "SmartBrowserContextStore",
    "SmartBrowserMcpServer",
    "SmartBrowserMcpTools",
    "serve_smart_browser_mcp",
    "smart_browser_tool_specs",
]
