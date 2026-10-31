"""The customer's alert for a verified phishing clone (architecture.md §15, product.md §11, K141).

``decision.apply`` queues ``customer_alert`` once a case becomes ``verified`` as a
``phishing_clone`` (by the rule or the operator). It goes once (``customer_alert_sent_at``), to
``customers.alert_email``; an account without that address gets none. While the customer is in
dry-run (or a test customer) it goes to ``DRY_RUN_REDIRECT_EMAIL`` instead, marked "[DRY RUN]".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select

from kalkan.config import Settings
from kalkan.db.models import Brand, Case, Customer, Domain
from kalkan.db.session import Database
from kalkan.email.messages import message
from kalkan.logging import get_logger
from kalkan.notices.gates import effective_dry_run
from kalkan.notices.sender import ProviderFactory

log = get_logger(__name__)


def send_customer_alert(
    database: Database,
    settings: Settings,
    provider: ProviderFactory,
    case_id: uuid.UUID,
    *,
    now: datetime,
) -> bool:
    """True when an alert went out now."""
    with database.worker_session() as session:
        case = session.get(Case, case_id, with_for_update=True)
        if case is None or case.customer_alert_sent_at is not None:
            return False
        customer = session.get_one(Customer, case.customer_id)
        if customer.is_demo or not customer.alert_email:
            return False
        domain = session.get_one(Domain, case.domain_id)
        brand = session.scalar(select(Brand.name).where(Brand.id == case.brand_id)) or ""
        email = message(
            "customer_alert",
            customer.alert_email,
            case_number=case.number,
            domain=domain.name,
            brand=brand,
            first_seen=case.first_seen_at.strftime("%Y-%m-%d %H:%M UTC"),
            case_url=f"{(settings.app_base_url or '').rstrip('/')}/threats/{case.number}",
            alert_email=customer.alert_email,
        )
        if effective_dry_run(settings, customer):
            if not settings.dry_run_redirect_email:
                log.warning("notices.alert_without_mailbox", case_id=str(case_id))
                return False
            email = email.__class__(
                to=settings.dry_run_redirect_email,
                subject=f"[DRY RUN] {email.subject}",
                text=f"[DRY RUN] For {customer.alert_email}.\n\n{email.text}",
                tag=email.tag,
            )
        provider().send(email)
        case.customer_alert_sent_at = now
    log.info("notices.customer_alert_sent", case_id=str(case_id))
    return True
