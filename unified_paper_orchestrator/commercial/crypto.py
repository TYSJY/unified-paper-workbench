"""Ed25519 helpers for purpose-bound commercial contracts."""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contracts import ContractError, SignedEnvelope, canonical_json_bytes


SIGNING_DOMAIN = b"unified-paper-orchestrator/signed-contract/v1\x00"


class SignatureError(ContractError):
    """A contract signature or trusted key is invalid."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise SignatureError("Signature is empty.")
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(
            f"{value}{padding}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise SignatureError("Signature is not valid base64url.") from exc


def public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return f"ed25519-{hashlib.sha256(raw).hexdigest()[:20]}"


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_to_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key(path: Path | str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise SignatureError("Private signing key could not be loaded.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("Signing key is not Ed25519.")
    return key


def load_public_key(path: Path | str) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise SignatureError("Trusted public key could not be loaded.") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("Trusted key is not Ed25519.")
    return key


def signature_input(envelope: SignedEnvelope) -> bytes:
    return SIGNING_DOMAIN + canonical_json_bytes(envelope.unsigned_dict())


def sign_payload(
    payload: dict,
    *,
    purpose: str,
    private_key: Ed25519PrivateKey,
    key_id: str | None = None,
) -> SignedEnvelope:
    resolved_key_id = key_id or public_key_id(private_key.public_key())
    unsigned = SignedEnvelope(
        purpose=purpose,
        key_id=resolved_key_id,
        payload=dict(payload),
        signature="pending",
    )
    signature = private_key.sign(signature_input(unsigned))
    return SignedEnvelope(
        purpose=purpose,
        key_id=resolved_key_id,
        payload=dict(payload),
        signature=_b64url_encode(signature),
    )


def verify_envelope(
    envelope: SignedEnvelope | dict,
    *,
    expected_purpose: str,
    trusted_keys: Mapping[str, Ed25519PublicKey],
) -> dict:
    parsed = envelope if isinstance(envelope, SignedEnvelope) else SignedEnvelope.from_dict(envelope)
    if parsed.purpose != expected_purpose:
        raise SignatureError(
            f"Signed contract purpose mismatch: expected {expected_purpose!r}."
        )
    key = trusted_keys.get(parsed.key_id)
    if key is None:
        raise SignatureError(f"Signing key {parsed.key_id!r} is not trusted.")
    try:
        key.verify(_b64url_decode(parsed.signature), signature_input(parsed))
    except InvalidSignature as exc:
        raise SignatureError("Signed contract verification failed.") from exc
    return dict(parsed.payload)
