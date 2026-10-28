"""Transactional delivery adapters for one-time product login codes."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol


class LoginCodeDeliveryError(RuntimeError):
    """The code was generated but could not be delivered."""


class LoginCodeSender(Protocol):
    def send_login_code(
        self,
        *,
        recipient: str,
        code: str,
        expires_at: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class SmtpLoginCodeSender:
    host: str
    port: int
    username: str
    password: str
    sender: str
    product_name: str = "Unified Paper Workbench"
    use_starttls: bool = True
    use_ssl: bool = False
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.host.strip() or not self.sender.strip():
            raise ValueError("SMTP host and sender are required.")
        if isinstance(self.port, bool) or not 1 <= int(self.port) <= 65535:
            raise ValueError("SMTP port is invalid.")
        if self.use_starttls and self.use_ssl:
            raise ValueError("SMTP STARTTLS and implicit SSL cannot both be enabled.")

    def send_login_code(
        self,
        *,
        recipient: str,
        code: str,
        expires_at: datetime,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = f"{self.product_name} login code"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            "\n".join(
                (
                    f"Your {self.product_name} verification code is: {code}",
                    "",
                    f"It expires at {expires_at.isoformat()}.",
                    "If you did not request this code, you can ignore this message.",
                )
            )
        )
        context = ssl.create_default_context()
        try:
            if self.use_ssl:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.host,
                    int(self.port),
                    timeout=float(self.timeout_seconds),
                    context=context,
                )
            else:
                client = smtplib.SMTP(
                    self.host,
                    int(self.port),
                    timeout=float(self.timeout_seconds),
                )
            with client:
                client.ehlo()
                if self.use_starttls:
                    client.starttls(context=context)
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise LoginCodeDeliveryError(
                "Verification e-mail could not be delivered."
            ) from exc
