"""E-mail providers (architecture.md §15, K139): Postmark through a mock transport, the choice
by ``ENV`` and the service's own messages."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import ipaddress
import json
import smtplib
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import message_from_bytes
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from kalkan.config import ConfigError
from kalkan.email.messages import invite_email, message, password_reset_email
from kalkan.email.provider import (
    POSTMARK_URL,
    Email,
    PermanentEmailError,
    PostmarkProvider,
    SmtpProvider,
    TemporaryEmailError,
    provider_for,
)
from tests.support import make_settings

SENDER = "abuse-reports@example.test"


def _postmark(status: int, body: dict[str, object], seen: list[httpx2.Request]) -> PostmarkProvider:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body)

    return PostmarkProvider(
        "server-token", SENDER, client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )


EMAIL = Email(
    to="abuse@registrar.example",
    subject="[KLK-000123] Phishing report: mintera-login.online",
    text="Body",
    reply_to="case+klk-000123@reply.example.test",
    tag="phishing",
    metadata={"case": "KLK-000123"},
)


def test_postmark_sends_plain_text_with_our_message_id() -> None:
    seen: list[httpx2.Request] = []
    sent = _postmark(200, {"MessageID": "pm-1", "ErrorCode": 0}, seen).send(EMAIL)
    [request] = seen
    assert str(request.url) == POSTMARK_URL
    assert request.headers["X-Postmark-Server-Token"] == "server-token"
    body = json.loads(request.content)
    assert body["From"] == SENDER
    assert body["To"] == "abuse@registrar.example"
    assert body["ReplyTo"] == EMAIL.reply_to
    assert body["TextBody"] == "Body"
    assert "HtmlBody" not in body
    assert body["MessageStream"] == "outbound"
    assert body["TrackLinks"] == "None"
    assert body["Headers"] == [{"Name": "Message-ID", "Value": sent.message_id}]
    assert sent.message_id.startswith("<")
    assert sent.message_id.endswith("@example.test>")
    assert sent.provider_id == "pm-1"


@pytest.mark.parametrize("code", [406, 300])
def test_an_inactive_recipient_or_invalid_message_is_permanent(code: int) -> None:
    with pytest.raises(PermanentEmailError):
        _postmark(422, {"ErrorCode": code, "Message": "no"}, []).send(EMAIL)


@pytest.mark.parametrize(
    ("status", "code"), [(401, 10), (422, 412), (422, 413), (429, 0), (500, 0)]
)
def test_account_and_service_errors_are_tried_again(status: int, code: int) -> None:
    with pytest.raises(TemporaryEmailError):
        _postmark(status, {"ErrorCode": code, "Message": "later"}, []).send(EMAIL)


def test_smtp_is_the_default_in_every_env(clean_env: pytest.MonkeyPatch) -> None:
    for env in ("local", "prod"):
        settings = make_settings(env=env, mail_from=SENDER, smtp_url="smtp://localhost:2525")
        assert settings.email_provider == "smtp"
        assert isinstance(provider_for(settings), SmtpProvider)
    with pytest.raises(ConfigError, match="SMTP_URL"):
        provider_for(make_settings(env="prod", mail_from=SENDER, postmark_server_token="t"))
    with pytest.raises(ConfigError, match="MAIL_FROM"):
        provider_for(make_settings(env="local", smtp_url="smtp://localhost:2525"))


def test_postmark_is_chosen_explicitly_needs_its_token_and_never_runs_locally(
    clean_env: pytest.MonkeyPatch,
) -> None:
    prod = make_settings(
        env="prod", email_provider="postmark", mail_from=SENDER, postmark_server_token="t"
    )
    assert isinstance(provider_for(prod), PostmarkProvider)
    with pytest.raises(ConfigError, match="POSTMARK_SERVER_TOKEN"):
        provider_for(make_settings(env="prod", email_provider="postmark", mail_from=SENDER))
    # A developer machine never reaches a real recipient through Postmark.
    with pytest.raises(ConfigError, match="ENV=local"):
        provider_for(
            make_settings(
                env="local", email_provider="postmark", mail_from=SENDER, postmark_server_token="t"
            )
        )
    with pytest.raises(ValidationError):
        make_settings(env="local", email_provider="sendmail")


def test_email_provider_is_read_from_the_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("EMAIL_PROVIDER", "postmark")
    assert make_settings(env="prod").email_provider == "postmark"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("smtp://mailpit", "plain://mailpit:25"),
        ("smtp://localhost:2525", "plain://localhost:2525"),
        ("smtp+starttls://relay.example", "starttls://relay.example:587"),
        ("SMTP+STARTTLS://u:p@relay.example:2587", "starttls://u:***@relay.example:2587"),
        ("smtps://u:p@relay.example", "tls://u:***@relay.example:465"),
    ],
)
def test_smtp_url_schemes_and_default_ports(url: str, shown: str) -> None:
    assert repr(SmtpProvider(url, SENDER)) == f"SmtpProvider({shown})"


@pytest.mark.parametrize(
    "url",
    [
        "http://relay.example",
        "smtp://",
        "smtp://relay.example:notaport",
        "smtp://user:s3cret-pw@relay.example",  # the password would cross the wire in clear
        "smtps://user@relay.example",
        "smtps://:s3cret-pw@relay.example",
    ],
)
def test_bad_smtp_urls_are_refused_without_echoing_them(url: str) -> None:
    with pytest.raises(ConfigError) as caught:
        SmtpProvider(url, SENDER)
    assert "s3cret-pw" not in str(caught.value)
    assert "relay.example" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    ["smtp://localhost:25", "smtp://127.0.0.1", "smtp://10.0.0.5:25", "smtp://postfix:25"],
)
def test_prod_allows_plain_smtp_to_a_nearby_relay(url: str) -> None:
    SmtpProvider(url, SENDER, env="prod")


def test_prod_refuses_plain_smtp_across_the_internet() -> None:
    with pytest.raises(ConfigError, match="SMTP_ALLOW_PLAINTEXT"):
        SmtpProvider("smtp://relay.example.net:25", SENDER, env="prod")
    with pytest.raises(ConfigError, match="SMTP_ALLOW_PLAINTEXT"):
        provider_for(
            make_settings(env="prod", mail_from=SENDER, smtp_url="smtp://93.184.216.34:25")
        )
    SmtpProvider("smtp://relay.example.net:25", SENDER, env="local")
    SmtpProvider("smtp://relay.example.net:25", SENDER, env="prod", allow_plaintext=True)
    allowed = make_settings(
        env="prod",
        mail_from=SENDER,
        smtp_url="smtp://relay.example.net:25",
        smtp_allow_plaintext=True,
    )
    assert isinstance(provider_for(allowed), SmtpProvider)


def test_the_service_messages() -> None:
    invite = invite_email(
        "new@customer.example",
        customer="Mintera Bank",
        inviter="owner@customer.example",
        url="https://app/x",
    )
    assert invite.subject == "You are invited to Mintera Bank's brand protection panel"
    assert "https://app/x" in invite.text
    assert "24 hours" in invite.text
    reset = password_reset_email("user@customer.example", url="https://app/reset")
    assert reset.subject == "Reset your password"
    alert = message(
        "customer_alert",
        "alerts@customer.example",
        case_number="KLK-000123",
        domain="mintera-login.online",
        brand="Mintera Bank",
        first_seen="2026-11-10 09:30 UTC",
        case_url="https://app/threats/KLK-000123",
        alert_email="alerts@customer.example",
    )
    assert alert.subject == "[KLK-000123] Phishing site found: mintera-login.online"
    assert not alert.text.startswith("Subject")


# --- SMTP against a fake server on 127.0.0.1 ---------------------------------------------------
# smtplib runs for real (EHLO, STARTTLS, implicit TLS, AUTH PLAIN, MAIL/RCPT/DATA); only the
# server is ours, so no test reaches the network. aiosmtpd is not a dependency, and the protocol
# subset smtplib speaks is small enough to answer here.

USER = "abuse@mintera.example"
PASSWORD = "p@ss:w/rd%-s3cret"  # every character URL-encoding must survive
REFUSED = "bounce@registrar.example"  # 550 to RCPT TO
DEFERRED = "busy@registrar.example"  # 450 to RCPT TO


@dataclass
class Mailbox:
    """What the fake server saw; the tests read it after ``send`` returns."""

    security: str  # plain, starttls or tls
    server_context: ssl.SSLContext
    received: list[bytes] = field(default_factory=list)
    envelopes: list[tuple[str, list[str]]] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    tls_used: list[bool] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def _reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def _wrap(self) -> None:
        self.wfile.flush()
        self.connection = self.request = self.server.box.server_context.wrap_socket(
            self.request, server_side=True
        )
        self.tls = True
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")

    def finish(self) -> None:
        super().finish()
        if self.tls:  # socketserver closes only the plain socket it accepted
            self.connection.close()

    def handle(self) -> None:
        self.tls = False
        # The client may give up mid-handshake (an untrusted certificate) or time out.
        with contextlib.suppress(OSError, ValueError):
            self._converse()

    def _converse(self) -> None:
        box = self.server.box
        if box.security == "tls":
            self._wrap()
        authed = False
        sender = ""
        recipients: list[str] = []
        self._reply("220 fake.test ESMTP")
        while line := self.rfile.readline():
            command = line.decode().strip()
            verb = command.split(" ", 1)[0].upper()
            if verb == "EHLO":
                extensions = []
                if box.security == "starttls" and not self.tls:
                    extensions.append("STARTTLS")
                if self.tls:
                    extensions.append("AUTH PLAIN")
                # The first line names the server; smtplib reads extensions from the rest.
                lines = ["fake.test", *extensions, "8BITMIME"]
                for extension in lines[:-1]:
                    self._reply(f"250-{extension}")
                self._reply(f"250 {lines[-1]}")
            elif verb == "STARTTLS":
                self._reply("220 ready")
                self._wrap()
            elif verb == "AUTH":
                _, mechanism, initial = command.split(" ", 2)
                _, user, password = base64.b64decode(initial).decode().split("\0")
                box.logins.append((user, password))
                if mechanism == "PLAIN" and (user, password) == (USER, PASSWORD):
                    authed = True
                    self._reply("235 ok")
                else:
                    self._reply("535 5.7.8 authentication failed")
            elif verb == "MAIL":
                if self.tls and not authed:
                    self._reply("530 authentication required")
                    continue
                sender = command.split(":", 1)[1].split()[0].strip("<>")
                recipients = []
                self._reply("250 ok")
            elif verb == "RCPT":
                recipient = command.split(":", 1)[1].strip().strip("<>")
                if recipient == REFUSED:
                    self._reply("550 5.1.1 no such user")
                elif recipient == DEFERRED:
                    self._reply("450 4.2.1 try later")
                else:
                    recipients.append(recipient)
                    self._reply("250 ok")
            elif verb == "DATA":
                self._reply("354 go on")
                data = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data += chunk
                box.received.append(data)
                box.envelopes.append((sender, recipients))
                box.tls_used.append(self.tls)
                self._reply("250 queued")
            elif verb == "QUIT":
                self._reply("221 bye")
                return
            else:  # RSET, NOOP
                self._reply("250 ok")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    box: Mailbox


@dataclass(frozen=True)
class Tls:
    server: ssl.SSLContext
    client: ssl.SSLContext


@pytest.fixture(scope="module")
def tls(tmp_path_factory: pytest.TempPathFactory) -> Tls:
    """A self-signed certificate for 127.0.0.1 and a client context that trusts only it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake smtp")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder: Path = tmp_path_factory.mktemp("smtp-tls")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (folder / "cert.pem").write_bytes(cert_pem)
    (folder / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    client = ssl.create_default_context(cadata=cert_pem.decode())
    return Tls(server, client)


@pytest.fixture
def smtp_server(tls: Tls, request: pytest.FixtureRequest) -> Iterator[tuple[int, Mailbox]]:
    box = Mailbox(security=request.param, server_context=tls.server)
    server = _Server(("127.0.0.1", 0), _Handler)
    server.box = box
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], box
    finally:
        server.shutdown()
        server.server_close()


def _url(scheme: str, port: int, *, password: str = PASSWORD) -> str:
    if scheme == "smtp":
        return f"smtp://127.0.0.1:{port}"
    return f"{scheme}://{quote(USER, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}"


SCHEMES = {"plain": "smtp", "starttls": "smtp+starttls", "tls": "smtps"}


@pytest.mark.parametrize("smtp_server", ["plain", "starttls", "tls"], indirect=True)
def test_smtp_sends_plain_text_with_our_message_id(
    smtp_server: tuple[int, Mailbox], tls: Tls
) -> None:
    port, box = smtp_server
    provider = SmtpProvider(_url(SCHEMES[box.security], port), SENDER, ssl_context=tls.client)
    sent = provider.send(EMAIL)
    [raw] = box.received
    parsed = message_from_bytes(raw)
    assert parsed["Message-ID"] == sent.message_id
    assert sent.message_id.startswith("<")
    assert sent.message_id.endswith("@example.test>")
    assert sent.provider_id is None
    assert parsed["From"] == SENDER
    assert parsed["To"] == "abuse@registrar.example"
    assert parsed["Reply-To"] == EMAIL.reply_to
    assert parsed["X-Kalkan-Tag"] == "phishing"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_payload(decode=True) == b"Body\r\n"  # CRLF on the wire
    assert box.envelopes == [(SENDER, ["abuse@registrar.example"])]
    assert box.tls_used == [box.security != "plain"]
    # The URL's percent-encoded credentials arrive decoded, and only over TLS.
    assert box.logins == ([] if box.security == "plain" else [(USER, PASSWORD)])


@pytest.mark.parametrize("smtp_server", ["starttls", "tls"], indirect=True)
def test_a_refused_login_is_tried_again_and_never_shows_the_password(
    smtp_server: tuple[int, Mailbox], tls: Tls
) -> None:
    port, box = smtp_server
    wrong = "wrong-s3cret"
    url = _url(SCHEMES[box.security], port, password=wrong)
    provider = SmtpProvider(url, SENDER, ssl_context=tls.client)
    with pytest.raises(TemporaryEmailError) as caught:
        provider.send(EMAIL)
    assert "535" in str(caught.value)
    for shown in (str(caught.value), repr(caught.value), repr(provider)):
        assert wrong not in shown
        assert quote(wrong, safe="") not in shown
    assert box.received == []


@pytest.mark.parametrize("smtp_server", ["starttls", "tls"], indirect=True)
def test_an_untrusted_certificate_is_refused(smtp_server: tuple[int, Mailbox]) -> None:
    port, box = smtp_server
    # The default context does not trust the fake server's self-signed certificate.
    with pytest.raises(TemporaryEmailError, match="SSL"):
        SmtpProvider(_url(SCHEMES[box.security], port), SENDER).send(EMAIL)
    assert box.logins == []
    assert box.received == []


@pytest.mark.parametrize("smtp_server", ["plain"], indirect=True)
def test_a_5xx_recipient_is_permanent_and_a_4xx_one_temporary(
    smtp_server: tuple[int, Mailbox],
) -> None:
    port, box = smtp_server
    provider = SmtpProvider(_url("smtp", port), SENDER)
    with pytest.raises(PermanentEmailError, match="550"):
        provider.send(Email(to=REFUSED, subject="s", text="t"))
    with pytest.raises(TemporaryEmailError, match="450"):
        provider.send(Email(to=DEFERRED, subject="s", text="t"))
    assert box.received == []


def test_an_unreachable_server_is_tried_again() -> None:
    with socket.socket() as probe:  # a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(TemporaryEmailError, match="SMTP"):
        SmtpProvider(f"smtp://127.0.0.1:{port}", SENDER).send(EMAIL)


def test_a_silent_server_times_out() -> None:
    with socket.socket() as silent:  # accepts the connection, never sends the greeting
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        provider = SmtpProvider(f"smtp://127.0.0.1:{port}", SENDER, timeout=0.3)
        with pytest.raises(TemporaryEmailError, match=r"TimeoutError|timed out"):
            provider.send(EMAIL)


def test_the_smtp_timeout_is_passed_to_smtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    class Refusing(smtplib.SMTP):
        def __init__(self, host: str, port: int, timeout: float) -> None:
            seen.append(timeout)
            raise ConnectionRefusedError

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    with pytest.raises(TemporaryEmailError):
        SmtpProvider("smtp://relay.example", SENDER).send(EMAIL)
    assert seen == [20.0]
