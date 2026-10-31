"""The service's own e-mails (architecture.md §15, §17): the customer's phishing alert, user
invitations and password resets. Block 14 sends the last two through the same provider.

Each template is plain text whose first line is ``Subject: ...``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from kalkan.email.provider import Email

LINK_HOURS = 24  # invitation and reset links (§17)

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,  # noqa: S701 - plain-text e-mail
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def message(name: str, to: str, **context: Any) -> Email:
    rendered = _env.get_template(f"{name}.txt.j2").render(**context)
    first, _, body = rendered.partition("\n")
    if not first.startswith("Subject: "):
        raise ValueError(f"{name}: the first line must be the subject")
    return Email(
        to=to, subject=first.removeprefix("Subject: ").strip(), text=body.lstrip("\n"), tag=name
    )


def invite_email(to: str, *, customer: str, inviter: str, url: str) -> Email:
    return message("invite", to, customer=customer, inviter=inviter, url=url, hours=LINK_HOURS)


def password_reset_email(to: str, *, url: str) -> Email:
    return message("password_reset", to, email=to, url=url, hours=LINK_HOURS)
