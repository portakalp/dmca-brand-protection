"""Sending e-mail (architecture.md §15; K9, K139).

:class:`EmailProvider` has two implementations, chosen by ``EMAIL_PROVIDER``:
:class:`SmtpProvider` (stdlib ``smtplib``; the default in every ``ENV``: Mailpit locally, any
SMTP relay on a self-hosted server) and :class:`PostmarkProvider` (the Email API, transactional
stream). ``ENV=local`` refuses Postmark, so a developer machine can never reach a real recipient
through it.

Every message is plain text. The RFC Message-ID is made here, before sending, and returned with
the provider's own id; inbound replies are matched on the Message-ID. Account and transport errors
(bad token or SMTP login, account pending approval, rate limit, 4xx/5xx of the service, no
connection) raise :class:`TemporaryEmailError`, so the job is tried again; a recipient that will
never accept the message (Postmark 406 or 300, an SMTP 5xx answer to RCPT TO) raises
:class:`PermanentEmailError`.
"""

from __future__ import annotations

import ipaddress
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx2

from kalkan.config import ConfigError, Settings

POSTMARK_URL = "https://api.postmarkapp.com/email"
TIMEOUT = 20.0
STREAM = "outbound"  # Postmark's transactional stream
PERMANENT_CODES = frozenset({300, 406})


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    reply_to: str | None = None
    tag: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Sent:
    message_id: str  # RFC 5322 Message-ID with its angle brackets
    provider_id: str | None  # Postmark's MessageID; None for SMTP


class EmailError(RuntimeError):
    pass


class TemporaryEmailError(EmailError):
    """Try again later: the provider or the account is not ready."""


class PermanentEmailError(EmailError):
    """This message will never go: a suppressed recipient or an invalid message."""


class EmailProvider(Protocol):
    def send(self, email: Email) -> Sent: ...


def _domain(sender: str) -> str:
    return sender.rsplit("@", 1)[-1].strip(" >")


def new_message_id(sender: str) -> str:
    # The domain is given: without it make_msgid asks DNS for this machine's name.
    return make_msgid(domain=_domain(sender))


class PostmarkProvider:
    def __init__(self, token: str, sender: str, *, client: httpx2.Client | None = None) -> None:
        self._token = token
        self._sender = sender
        self._client = client or httpx2.Client(timeout=TIMEOUT)

    def send(self, email: Email) -> Sent:
        message_id = new_message_id(self._sender)
        body: dict[str, Any] = {
            "From": self._sender,
            "To": email.to,
            "Subject": email.subject,
            "TextBody": email.text,
            "MessageStream": STREAM,
            "Headers": [{"Name": "Message-ID", "Value": message_id}],
            "TrackOpens": False,
            "TrackLinks": "None",
        }
        if email.reply_to:
            body["ReplyTo"] = email.reply_to
        if email.tag:
            body["Tag"] = email.tag
        if email.metadata:
            body["Metadata"] = email.metadata
        try:
            response = self._client.post(
                POSTMARK_URL,
                json=body,
                headers={"Accept": "application/json", "X-Postmark-Server-Token": self._token},
            )
        except httpx2.HTTPError as exc:
            raise TemporaryEmailError(f"Postmark unreachable: {type(exc).__name__}") from exc
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        code = int(answer.get("ErrorCode") or 0) if isinstance(answer, dict) else 0
        if response.status_code == 200 and code == 0:
            return Sent(message_id, str(answer.get("MessageID") or "") or None)
        text = f"Postmark {response.status_code}, ErrorCode {code}: {answer.get('Message', '')}"
        if response.status_code == 422 and code in PERMANENT_CODES:
            raise PermanentEmailError(text[:300])
        raise TemporaryEmailError(text[:300])


def _nearby_host(host: str) -> bool:
    """Loopback, a private or link-local address, or a single-label name (a compose service)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host.rstrip(".") or host.lower() == "localhost"
    return address.is_loopback or address.is_private or address.is_link_local


# SMTP_URL scheme -> (default port, transport security). Plain smtp:// is for a relay on the
# same host or network (Mailpit, a local MTA); anything across the internet uses TLS.
SMTP_SCHEMES: dict[str, tuple[int, str]] = {
    "smtp": (25, "plain"),
    "smtp+starttls": (587, "starttls"),
    "smtps": (465, "tls"),
}


class SmtpProvider:
    """SMTP delivery for ``smtp://``, ``smtp+starttls://`` and ``smtps://`` URLs.

    ``user:password@`` in the URL is percent-decoded and sent with AUTH after TLS is up. The
    password is kept out of every message and of ``repr``: SMTP_URL holds a secret, and errors
    end up in logs and in a notice's ``failure_reason``.
    """

    def __init__(
        self,
        url: str,
        sender: str,
        *,
        timeout: float = TIMEOUT,
        ssl_context: ssl.SSLContext | None = None,
        env: str = "local",
        allow_plaintext: bool = False,
    ) -> None:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        try:
            port = parts.port
        except ValueError:
            port = None
            scheme = ""  # an invalid port: reported below without echoing the URL
        if scheme not in SMTP_SCHEMES or not parts.hostname:
            raise ConfigError(
                "SMTP_URL must look like smtp://host:port, smtp+starttls://user:pass@host:587"
                " or smtps://user:pass@host:465"
            )
        default_port, security = SMTP_SCHEMES[scheme]
        user = unquote(parts.username) if parts.username else None
        password = unquote(parts.password) if parts.password is not None else None
        if security == "plain" and (user or password):
            # AUTH over a plain connection would send the password in the clear.
            raise ConfigError("SMTP_URL credentials need smtp+starttls:// or smtps://")
        if (user is None) != (password is None):
            raise ConfigError("SMTP_URL needs both a user and a password, or neither")
        if (
            security == "plain"
            and env == "prod"
            and not allow_plaintext
            and not _nearby_host(parts.hostname)
        ):
            # Notices and password resets would cross the internet in clear text.
            raise ConfigError(
                "SMTP_URL smtp:// is only for a relay on this host or network when ENV=prod;"
                " use smtp+starttls:// or smtps://, or set SMTP_ALLOW_PLAINTEXT=true"
            )
        self._host = parts.hostname
        self._port = port or default_port
        self._security = security
        self._user = user
        self._password = password
        self._sender = sender
        self._timeout = timeout
        # Certificates and host names are verified; tests pass a context trusting their own CA.
        self._ssl_context = ssl_context

    def __repr__(self) -> str:
        auth = f"{self._user}:***@" if self._user else ""
        return f"SmtpProvider({self._security}://{auth}{self._host}:{self._port})"

    def _context(self) -> ssl.SSLContext:
        return self._ssl_context or ssl.create_default_context()

    def _connect(self) -> smtplib.SMTP:
        if self._security == "tls":
            return smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=self._context()
            )
        smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        if self._security == "starttls":
            try:
                smtp.starttls(context=self._context())
            except BaseException:
                smtp.close()
                raise
        return smtp

    def send(self, email: Email) -> Sent:
        message = EmailMessage()
        message_id = new_message_id(self._sender)
        message["From"] = self._sender
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Message-ID"] = message_id
        if email.reply_to:
            message["Reply-To"] = email.reply_to
        if email.tag:
            message["X-Kalkan-Tag"] = email.tag
        message.set_content(email.text)
        try:
            with self._connect() as smtp:
                if self._user is not None and self._password is not None:
                    smtp.login(self._user, self._password)
                smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            text = f"SMTP recipient refused: {codes}"
            if codes and all(code >= 500 for code in codes):
                raise PermanentEmailError(text) from exc
            raise TemporaryEmailError(text) from exc
        except smtplib.SMTPAuthenticationError as exc:
            # The server's answer only; the exception never carries the password.
            raise TemporaryEmailError(f"SMTP login refused: {exc.smtp_code}") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise TemporaryEmailError(f"SMTP: {type(exc).__name__}: {exc}"[:300]) from exc
        return Sent(message_id, None)


def provider_for(settings: Settings) -> EmailProvider:
    """The provider ``EMAIL_PROVIDER`` names; Postmark is refused when ``ENV=local`` (§15)."""
    sender = settings.mail_from
    if not sender:
        raise ConfigError("MAIL_FROM is required to send e-mail")
    if settings.email_provider == "smtp":
        if not settings.smtp_url:
            raise ConfigError("SMTP_URL is required when EMAIL_PROVIDER=smtp")
        return SmtpProvider(
            settings.smtp_url.get_secret_value(),
            sender,
            env=settings.env,
            allow_plaintext=settings.smtp_allow_plaintext,
        )
    if settings.env == "local":
        raise ConfigError("EMAIL_PROVIDER=postmark is refused when ENV=local; use smtp (Mailpit)")
    if settings.postmark_server_token is None:
        raise ConfigError("POSTMARK_SERVER_TOKEN is required when EMAIL_PROVIDER=postmark")
    return PostmarkProvider(settings.postmark_server_token.get_secret_value(), sender)
