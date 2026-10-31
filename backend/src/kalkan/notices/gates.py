"""The sending gates (architecture.md §15; K28, K42).

In this order; the first that holds is the notice's ``blocked_reason`` and, while the case is
``approved``, the case's too:

1. the customer is monitor-only → ``monitor_only`` (no preview either);
2. the customer's sending is off → ``sending_disabled``;
3. the customer is suspended by the operator → ``suspended``;
4. dry-run (global ``NOTICES_DRY_RUN``, the customer's ``dry_run`` or ``is_test``) → the
   preview goes to ``DRY_RUN_REDIRECT_EMAIL``, then ``dry_run``;
5. the global send switch (``settings.sending_enabled``) is off → ``kill_switch`` (previews
   are not stopped by it);
6. the channel's real notices of the UTC day reached its limit → ``daily_limit``;
7. no LOA on file → ``no_loa``;
8. the template's legal text is not approved yet → ``legal_review``.

Gates 1-3 are also checked before any AI text is written (``notices.text``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kalkan.config import Settings
from kalkan.db.models import Customer, Notice
from kalkan.domain.enums import BlockedReason, Channel, CustomerStatus, NoticeStatus

# A real notice counts against the day's limit once it went out or was handed to the operator.
COUNTED = frozenset(
    {NoticeStatus.SENT, NoticeStatus.ACKNOWLEDGED, NoticeStatus.RESOLVED, NoticeStatus.RETRACTED}
)


def effective_dry_run(settings: Settings, customer: Customer) -> bool:
    return settings.notices_dry_run or customer.dry_run or customer.is_test


def customer_gate(customer: Customer) -> BlockedReason | None:
    """Gates 1-3."""
    if customer.monitor_only:
        return BlockedReason.MONITOR_ONLY
    if not customer.sending_enabled:
        return BlockedReason.SENDING_DISABLED
    if customer.status is CustomerStatus.SUSPENDED:
        return BlockedReason.SUSPENDED
    return None


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def limit_reached(
    session: Session,
    channel: Channel,
    limits: dict[str, Any],
    defaults: dict[str, Any],
    now: datetime,
) -> bool:
    """Gate 6. Serialised per channel with a transaction lock, so two workers cannot both take
    the last place of the day."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"notice-limit:{channel}"}
    )
    limit = limits.get(channel.value, defaults.get(channel.value))
    if limit is None:
        return False
    start = day_start(now)
    sent = session.scalar(
        select(func.count(Notice.id)).where(
            Notice.channel == channel,
            Notice.dry_run.is_(False),
            Notice.sent_at >= start,
            Notice.sent_at < start + timedelta(days=1),
            Notice.status.in_(COUNTED),
        )
    )
    return int(sent or 0) >= int(limit)
