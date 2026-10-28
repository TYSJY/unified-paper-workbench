from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, QLocale, QSettings, Signal, Slot

from ..config import STATE_DIR


DEFAULT_LOCALE = "zh_CN"
FALLBACK_LOCALE = "zh_CN"


def _locale_dir() -> Path:
    return Path(__file__).resolve().parent / "locales"


def _lookup(catalog: dict[str, Any], key: str) -> Any:
    value: Any = catalog
    for part in str(key or "").split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _normalize_locale(value: Any, available: set[str]) -> str:
    token = str(value or "").strip().replace("-", "_")
    if token in available:
        return token
    folded = token.casefold()
    for candidate in available:
        if candidate.casefold() == folded:
            return candidate
    language = folded.split("_", 1)[0]
    for candidate in sorted(available):
        if candidate.casefold().split("_", 1)[0] == language:
            return candidate
    return FALLBACK_LOCALE if FALLBACK_LOCALE in available else sorted(available)[0]


class I18nManager(QObject):
    """Runtime translation catalog shared by QML and Python read models.

    Locale files are ordinary UTF-8 JSON documents.  Adding another language
    requires only one complete catalog; no QML or controller branch is needed.
    """

    languageChanged = Signal()

    def __init__(self, *, settings_path: Path | str | None = None) -> None:
        super().__init__()
        self._catalogs = self._load_catalogs()
        if not self._catalogs:
            raise RuntimeError("No Qt workbench locale catalogs were found")
        self._settings_path = Path(
            settings_path
            or (Path(STATE_DIR) / "ui" / "workbench_preferences.ini")
        )
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = QSettings(
            str(self._settings_path),
            QSettings.Format.IniFormat,
        )
        saved = self._settings.value("appearance/language", "")
        system = QLocale.system().name()
        self._locale = _normalize_locale(
            saved or system or DEFAULT_LOCALE,
            set(self._catalogs),
        )
        self._revision = 0

    @staticmethod
    def _load_catalogs() -> dict[str, dict[str, Any]]:
        catalogs: dict[str, dict[str, Any]] = {}
        for path in sorted(_locale_dir().glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError(f"Locale catalog root must be an object: {path}")
            meta = payload.get("_meta", {})
            code = str(meta.get("code") or path.stem).strip()
            if not code:
                raise ValueError(f"Locale catalog has no code: {path}")
            catalogs[code] = payload
        return catalogs

    @Property(str, notify=languageChanged)
    def locale(self) -> str:
        return self._locale

    @Property(int, notify=languageChanged)
    def revision(self) -> int:
        return self._revision

    @Property("QVariantList", constant=True)
    def languages(self) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for code, catalog in self._catalogs.items():
            meta = catalog.get("_meta", {})
            options.append(
                {
                    "code": code,
                    "label": str(meta.get("label") or code),
                    "shortLabel": str(meta.get("short_label") or code.split("_", 1)[0].upper()),
                }
            )
        return options

    def translate(self, key: str, default: str = "", **values: Any) -> str:
        value = _lookup(self._catalogs.get(self._locale, {}), key)
        if not isinstance(value, str):
            value = _lookup(self._catalogs.get(FALLBACK_LOCALE, {}), key)
        text = value if isinstance(value, str) else (default or str(key))
        if values:
            try:
                return text.format(**values)
            except (KeyError, ValueError):
                return text
        return text

    @Slot(str, result=str)
    @Slot(str, int, result=str)
    def text(self, key: str, _revision: int = 0) -> str:
        return self.translate(key)

    @Slot(str, "QVariantMap", int, result=str)
    def formatText(  # noqa: N802 - QML API
        self,
        key: str,
        values: dict[str, Any],
        _revision: int = 0,
    ) -> str:
        return self.translate(key, **dict(values or {}))

    @Slot(str)
    def setLocale(self, locale: str) -> None:  # noqa: N802 - QML API
        selected = _normalize_locale(locale, set(self._catalogs))
        if selected == self._locale:
            return
        self._locale = selected
        self._revision += 1
        self._settings.setValue("appearance/language", selected)
        self._settings.sync()
        self.languageChanged.emit()


__all__ = ["DEFAULT_LOCALE", "FALLBACK_LOCALE", "I18nManager"]
