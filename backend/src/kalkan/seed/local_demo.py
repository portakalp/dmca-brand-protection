"""The one-command local demo (``make demo``): an operator account next to the seeded workspace.

``kalkan seed`` writes the Mintera Bank users to the credentials file; the demo also needs an
ops panel operator, and someone trying Kalkan on a laptop needs the current TOTP code of each
account without installing an authenticator app. TOTP stays mandatory (it is part of what the
demo shows), so this module only makes the secrets the seed already wrote easy to use:

- :func:`reset_demo_operator` creates ``operator@kalkan.local`` or, on a re-run, gives it a new
  password and TOTP secret and ends its sessions, exactly as the seed renews the customer users.
- :func:`add_staff` records the operator in the same git-ignored credentials file, so one file
  holds every demo secret and ``kalkan demo code`` can read any of them.
- :func:`current_code` turns a stored secret into the code an authenticator would show.

Nothing here may run with ``ENV=prod``: there the credentials file must never exist (the seed
refuses it) and an operator with a printed password would be a back door. The CLI checks it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyotp
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import AuditLog, StaffSession, StaffUser
from kalkan.security import encrypt_totp_secret, hash_password, new_password, new_totp_secret

DEMO_OPERATOR_EMAIL = "operator@kalkan.local"


class DemoCredentialsError(RuntimeError):
    """The credentials file is missing, unreadable or has no entry for the address."""


@dataclass(frozen=True)
class DemoOperator:
    email: str
    password: str
    totp_secret: str
    created: bool


def reset_demo_operator(
    session: Session, settings: Settings, *, actor: str, email: str = DEMO_OPERATOR_EMAIL
) -> DemoOperator:
    """Create the demo operator, or renew its password and TOTP secret.

    A re-run must not fail on the existing account (``admin.create_staff_user`` refuses
    duplicates) and must not leave the previous secrets working: the old sessions are deleted
    and a disabled account is enabled again, because the demo just printed its new password.
    """
    password = new_password()
    secret = new_totp_secret()
    user = session.scalar(select(StaffUser).where(StaffUser.email == email))
    created = user is None
    if user is None:
        user = StaffUser(email=email, password_hash="", totp_secret_enc="")
        session.add(user)
    user.password_hash = hash_password(password)
    user.totp_secret_enc = encrypt_totp_secret(settings, secret)
    user.disabled_at = None
    session.flush()
    if not created:
        session.execute(delete(StaffSession).where(StaffSession.staff_user_id == user.id))
    action = "staff.create" if created else "staff.reset"
    session.add(AuditLog(actor=actor, action=action, target=email, data={"demo": True}))
    return DemoOperator(email, password, secret, created)


def load_credentials(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DemoCredentialsError(f"{path} does not exist; run the demo setup first") from None
    except (OSError, ValueError) as exc:
        raise DemoCredentialsError(f"cannot read {path}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise DemoCredentialsError(f"{path} is not a credentials file")
    return data


def add_staff(path: Path, operator: DemoOperator) -> None:
    """Record the operator next to the seeded users (replacing an older entry for it)."""
    data = load_credentials(path)
    staff = [
        entry
        for entry in data.get("staff", [])
        if isinstance(entry, dict) and entry.get("email") != operator.email
    ]
    staff.append(
        {
            "role": "operator",
            "email": operator.email,
            "password": operator.password,
            "totp_secret": operator.totp_secret,
        }
    )
    data["staff"] = staff
    _write_private(path, data)


def accounts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every account in the file: the customer users first, then the operators."""
    found = [*data.get("users", []), *data.get("staff", [])]
    return [entry for entry in found if isinstance(entry, dict) and "email" in entry]


def totp_secret_for(data: dict[str, Any], email: str) -> str:
    wanted = email.strip().lower()
    for entry in accounts(data):
        if str(entry["email"]).lower() == wanted and entry.get("totp_secret"):
            return str(entry["totp_secret"])
    known = ", ".join(str(entry["email"]) for entry in accounts(data)) or "none"
    raise DemoCredentialsError(f"no demo account {email!r} (known: {known})")


def current_code(secret: str, at: datetime | None = None) -> tuple[str, int]:
    """The code an authenticator shows now and the seconds it stays valid."""
    totp = pyotp.TOTP(secret)
    moment = int((at or datetime.now(UTC)).timestamp())
    remaining = totp.interval - moment % totp.interval
    return totp.at(moment), remaining


def _write_private(path: Path, data: dict[str, Any]) -> None:
    """Replace the file atomically with mode 0600: it holds passwords and TOTP secrets."""
    # mkstemp creates a new file (O_EXCL) with mode 0600 under an unpredictable name.
    descriptor, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
