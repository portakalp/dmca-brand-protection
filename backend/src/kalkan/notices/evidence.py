"""Evidence links (architecture.md §11, §17; K140).

Every notice gets its own link when its text is rendered: 32 random bytes in the URL, only
their SHA-256 in ``evidence_links.token_hash``, valid for 90 days. The token appears in the
notice body and nowhere else; the API resolves it with ``resolve_evidence_link`` and reads the
case under row level security (``api/evidence.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from kalkan.config import ConfigError, Settings
from kalkan.db.models import Case, EvidenceLink
from kalkan.security import hash_token, new_token

LIFETIME = timedelta(days=90)


def create_link(
    session: Session, settings: Settings, case: Case, *, now: datetime
) -> tuple[EvidenceLink, str]:
    """A new link for ``case`` and its URL."""
    if not settings.evidence_base_url:
        raise ConfigError("EVIDENCE_BASE_URL is required to write notices")
    token = new_token()
    link = EvidenceLink(
        customer_id=case.customer_id,
        case_id=case.id,
        token_hash=hash_token(token),
        expires_at=now + LIFETIME,
    )
    session.add(link)
    session.flush()
    return link, f"{settings.evidence_base_url.rstrip('/')}/e/{token}"
