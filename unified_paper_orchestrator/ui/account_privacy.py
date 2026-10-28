"""Presentation-only masking. Never pass masked strings to the account store."""
from __future__ import annotations

import re
from typing import Any

MASK = "••••••"
_EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
_ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", re.I)
_IDENTITY_KEYS = {"username", "email", "login", "login_name", "login_account", "orcid", "phone", "account_id", "record_key", "username_candidates", "email_candidates"}


def identity_tokens(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _IDENTITY_KEYS:
                candidates = item if isinstance(item, (list, tuple)) else [item]
                result.update(str(v).strip() for v in candidates if isinstance(v, (str, int)) and str(v).strip())
            result.update(identity_tokens(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.update(identity_tokens(item))
    return result


def mask_personal(value: Any, tokens: set[str] | None = None) -> str:
    text = str(value or "")
    # Match full tokens before the generic email rule, including tokens in
    # labels, diagnostic identifiers and URL parameters.
    if tokens:
        pattern = "|".join(re.escape(token) for token in sorted(tokens, key=len, reverse=True) if token)
        if pattern:
            text = re.sub(pattern, lambda _: MASK, text, flags=re.I)
    text = _EMAIL.sub(MASK, text)
    text = _ORCID.sub(MASK, text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1" + MASK + "@", text, flags=re.I)
    # Query strings/fragments may contain login hints, tokens or identifiers.
    return re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1?" + MASK, text, flags=re.I)
