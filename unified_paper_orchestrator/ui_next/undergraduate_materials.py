"""Qt attachment draft; file IO runs off the GUI thread and uses no AI."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QObject, Property, Signal, Slot, QThreadPool, QUrl
from ..undergraduate_materials import import_materials, normalize_attachments, verify_attachments


class UndergraduateMaterials(QObject):
    changed = Signal()

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = root
        self._items, self._warnings = [], []
        self._busy = False

    @Property("QVariantList", notify=changed)
    def items(self):
        return list(self._items)

    @Property(str, notify=changed)
    def warnings(self):
        return "\n".join(self._warnings)

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Slot()
    def reset(self):
        if not self._busy:
            self._items, self._warnings = [], []
            self.changed.emit()

    @Slot("QVariantList", str)
    def add(self, urls, role):
        if self._busy:
            return
        paths = [QUrl(str(url)).toLocalFile() or str(url) for url in urls]
        if not paths:
            return
        from .controller import _Worker
        self._busy = True
        self.changed.emit()
        worker = _Worker("undergraduate_materials", lambda: import_materials(paths, self.root, role))
        worker.signals.completed.connect(self._completed)
        worker.signals.failed.connect(self._failed)
        QThreadPool.globalInstance().start(worker)

    @Slot(str, object, float)
    def _completed(self, _key, result, _duration):
        self._warnings = result["warnings"]
        seen = {(item["sha256"], item["role"]) for item in self._items}
        for item in result["items"]:
            if (item["sha256"], item["role"]) in seen:
                self._warnings.append(f"{item['name']}：重复资料，已跳过")
                continue
            try:
                normalize_attachments(self._items + [item])
            except ValueError:
                self._warnings.append(f"{item['name']}：超过本论文附件总限制，未加入")
                continue
            self._items.append(item)
            seen.add((item["sha256"], item["role"]))
        self._busy = False
        self.changed.emit()

    @Slot(str, str, str, str, float)
    def _failed(self, *_args):
        self._warnings = ["导入未完成，请检查文件是否可访问、磁盘空间是否充足后重试。"]
        self._busy = False
        self.changed.emit()

    @Slot(str)
    def remove(self, identifier):
        if not self._busy:
            # Unlink only from this draft. Originals and stored copies are retained.
            self._items = [item for item in self._items if item["id"] != identifier]
            self.changed.emit()

    @Slot(str, result=str)
    def preview(self, identifier):
        item = next((i for i in self._items if i["id"] == identifier), None)
        if item is None:
            return "资料不存在"
        try:
            verify_attachments([item], self.root)
            text = Path(item["text_path"]).read_text(encoding="utf-8")
            return (item["name"] + "\n" + item["detail"] + "\n\n"
                    + (text[:15000] if text else "暂无提取文字，请核对原件或补充文字版。")
                    + ("\n\n[预览仅显示前 15000 字，完整提取文字已保存]" if len(text) > 15000 else ""))
        except (ValueError, OSError):
            return "资料副本已变化或不可读，请移除后重新导入。"

    def snapshot(self):
        if self._busy:
            raise ValueError("资料仍在导入中，请等待完成")
        result = normalize_attachments(self._items)
        verify_attachments(result, self.root)
        return result
