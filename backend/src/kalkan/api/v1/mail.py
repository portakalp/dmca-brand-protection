"""E-mail from the API: invitations and password resets (§17), through the same
``EmailProvider`` as the notices (SMTP and Mailpit locally, Postmark in prod; K139).

A failed send is logged and the request still succeeds: the link can be sent again.
"""

from __future__ import annotations

from fastapi import Request

from kalkan.email.provider import Email, EmailProvider, provider_for
from kalkan.logging import get_logger

log = get_logger(__name__)


def mailer(request: Request) -> EmailProvider:
    state = request.app.state
    provider: EmailProvider | None = getattr(state, "mailer", None)
    if provider is None:
        provider = provider_for(state.settings)
        state.mailer = provider
    return provider


def send_mail(request: Request, email: Email) -> bool:
    try:
        mailer(request).send(email)
    except Exception as exc:  # noqa: BLE001 - the link can be sent again
        log.warning("api.mail_failed", tag=email.tag, error=type(exc).__name__)
        return False
    return True
