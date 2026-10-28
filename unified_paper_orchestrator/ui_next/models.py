from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QAbstractListModel, QModelIndex, Qt, Signal, Slot


class PaperListModel(QAbstractListModel):
    """Virtualized paper rows exposed to QML.

    The source list stays independent from the visible filtered list so search,
    page and status changes never trigger another filesystem projection.
    """

    countChanged = Signal()

    _ROLE_NAMES = (
        "paperId",
        "title",
        "currentStage",
        "stageKey",
        "status",
        "statusLabel",
        "statusTone",
        "nextAction",
        "nextActionLabel",
        "progress",
        "stepLabel",
        "queueLabel",
        "updatedAt",
        "detail",
        "writingProgress",
        "figuresProgress",
        "journalProgress",
        "submissionProgress",
        "isActive",
        "isWaiting",
        "isBlocked",
        "isComplete",
    )

    def __init__(self) -> None:
        super().__init__()
        self._source: list[dict[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._page = "overview"
        self._query = ""
        self._status_filter = "all"
        self._roles = {
            Qt.UserRole + index + 1: name.encode("utf-8")
            for index, name in enumerate(self._ROLE_NAMES)
        }

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802 - Qt API
        return self._roles

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._rows):
            return None
        role_name = self._roles.get(role)
        if role_name is None:
            return None
        return self._rows[index.row()].get(role_name.decode("utf-8"))

    def replace(self, rows: list[dict[str, Any]]) -> None:
        self._source = [dict(row) for row in rows]
        self._apply_filters()

    def configure(self, *, page: str | None = None, query: str | None = None, status: str | None = None) -> None:
        changed = False
        if page is not None and page != self._page:
            self._page = page
            changed = True
        if query is not None and query.casefold().strip() != self._query:
            self._query = query.casefold().strip()
            changed = True
        if status is not None and status != self._status_filter:
            self._status_filter = status
            changed = True
        if changed:
            self._apply_filters()

    def _apply_filters(self) -> None:
        old_count = len(self._rows)
        filtered: list[dict[str, Any]] = []
        for source_row in self._source:
            row = dict(source_row)
            if self._page != "overview" and not bool(row.get(f"visible_{self._page}")):
                continue
            if self._page != "overview":
                prefix = self._page
                row["currentStage"] = row.get(
                    f"{prefix}StageLabel", row.get("currentStage")
                )
                row["status"] = row.get(f"{prefix}Status", row.get("status"))
                row["statusLabel"] = row.get(f"{prefix}StatusLabel", row.get("statusLabel"))
                row["statusTone"] = row.get(f"{prefix}StatusTone", row.get("statusTone"))
                row["stepLabel"] = row.get(f"{prefix}StepLabel", row.get("stepLabel"))
                row["progress"] = row.get(f"{prefix}Progress", row.get("progress"))
                row["detail"] = row.get(f"{prefix}Detail", row.get("detail"))
                row["nextActionLabel"] = row.get(
                    f"{prefix}NextActionLabel", row.get("nextActionLabel")
                )
                row["isActive"] = row.get("status") in {"creating_thread", "starting", "running"}
                row["isWaiting"] = row.get("status") in {
                    "ready", "queued", "waiting", "waiting_event", "paused", "needs_review",
                    "waiting_data", "waiting_approval", "needs_user", "manual", "manual_required",
                }
                row["isBlocked"] = row.get("status") in {
                    "blocked", "stopped", "missing_upstream", "migration_review", "rejected",
                }
                row["isComplete"] = row.get("status") in {
                    "complete", "submitted", "active", "accepted", "monitoring", "selection_complete",
                }
            if self._status_filter != "all":
                if self._status_filter == "active" and not row.get("isActive"):
                    continue
                if self._status_filter == "waiting" and not row.get("isWaiting"):
                    continue
                if self._status_filter == "blocked" and not row.get("isBlocked"):
                    continue
                if self._status_filter == "complete" and not row.get("isComplete"):
                    continue
            haystack = str(row.get("searchText") or "").casefold()
            if self._query and self._query not in haystack:
                continue
            filtered.append(row)
        self.beginResetModel()
        self._rows = filtered
        self.endResetModel()
        if old_count != len(self._rows):
            self.countChanged.emit()

    @Slot(int, result="QVariantMap")
    def get(self, index: int) -> dict[str, Any]:
        if 0 <= int(index) < len(self._rows):
            return dict(self._rows[int(index)])
        return {}

    @Slot(str, result=int)
    def indexOf(self, paper_id: str) -> int:  # noqa: N802 - QML API
        for index, row in enumerate(self._rows):
            if str(row.get("paperId") or "") == str(paper_id or ""):
                return index
        return -1

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)
