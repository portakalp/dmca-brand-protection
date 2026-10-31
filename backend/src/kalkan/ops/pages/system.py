"""System status: queues, outbox, this month's AI and proxy spend (architecture.md §19)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from kalkan.ops import auth, status
from kalkan.ops.web import current_staff, now, render, services, transaction

router = APIRouter(prefix="/system")


@router.get("")
def system(request: Request, _: Annotated[auth.Staff, Depends(current_staff)]) -> Response:
    found = services(request)
    at = now()
    with transaction(request) as session:
        ai = status.ai_spend(session, at)
        stuck = status.stuck_outbox(session)
    return render(
        request,
        "system.html",
        {
            "queues": status.queue_depths(found.redis),
            "ai": ai,
            "ai_budget": found.settings.ai_monthly_budget_usd,
            "proxy": status.proxy_gb(found.redis, at),
            "proxy_limit": found.settings.proxy_daily_gb_limit,
            "stuck_outbox": stuck,
            "month": status.month_start(at).strftime("%B %Y"),
        },
    )
