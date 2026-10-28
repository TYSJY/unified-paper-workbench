"""Run the loopback-only development control plane."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from unified_paper_orchestrator.commercial.crypto import (
    generate_private_key,
    load_private_key,
    private_key_to_pem,
    public_key_id,
    public_key_to_pem,
)
from unified_paper_orchestrator.io_utils import write_bytes_atomic

from .http_server import ControlPlaneHttpServer
from .mail_delivery import SmtpLoginCodeSender
from .service import ControlPlaneConfig, ControlPlaneService
from .store import ControlPlaneStore


def _load_or_create_key(path: Path):
    if path.exists():
        return load_private_key(path)
    key = generate_private_key()
    write_bytes_atomic(path, private_key_to_pem(key))
    write_bytes_atomic(path.with_suffix(".public.pem"), public_key_to_pem(key.public_key()))
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified Paper local commercial control plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--state-dir", type=Path, default=Path(".commercial") / "control-plane")
    parser.add_argument(
        "--expose-development-codes",
        action="store_true",
        help="Return one-time login codes in development API responses.",
    )
    parser.add_argument(
        "--allowed-browser-origin",
        action="append",
        default=[],
        help="Exact browser origin allowed to call the account API; repeat as needed.",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("The bundled development server may bind only to loopback.")
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    key = _load_or_create_key(state_dir / "development-signing-key.pem")
    issuer = f"http://{args.host}:{args.port}"
    smtp_host = str(os.environ.get("UNIFIED_PAPER_SMTP_HOST") or "").strip()
    login_code_sender = None
    if smtp_host:
        login_code_sender = SmtpLoginCodeSender(
            host=smtp_host,
            port=int(os.environ.get("UNIFIED_PAPER_SMTP_PORT") or "587"),
            username=str(os.environ.get("UNIFIED_PAPER_SMTP_USERNAME") or ""),
            password=str(os.environ.get("UNIFIED_PAPER_SMTP_PASSWORD") or ""),
            sender=str(os.environ.get("UNIFIED_PAPER_SMTP_SENDER") or "").strip(),
            use_starttls=str(
                os.environ.get("UNIFIED_PAPER_SMTP_STARTTLS") or "1"
            ).strip()
            not in {"0", "false", "False"},
            use_ssl=str(
                os.environ.get("UNIFIED_PAPER_SMTP_SSL") or "0"
            ).strip()
            in {"1", "true", "True"},
        )
    service = ControlPlaneService(
        ControlPlaneStore(state_dir / "control-plane.sqlite3"),
        key,
        config=ControlPlaneConfig(
            issuer=issuer,
            expose_development_codes=args.expose_development_codes,
            allowed_browser_origins=tuple(
                str(item).strip().rstrip("/")
                for item in args.allowed_browser_origin
                if str(item).strip()
            ),
        ),
        login_code_sender=login_code_sender,
    )
    server = ControlPlaneHttpServer((args.host, args.port), service)
    print(
        f"Development control plane: {issuer} | signing key: {public_key_id(key.public_key())}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
