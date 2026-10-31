"""Cases: search and detail (architecture.md §19)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import or_, select

from kalkan.db.models import (
    Artifact,
    Brand,
    Case,
    CaseEvent,
    Customer,
    Domain,
    Label,
    Scan,
)
from kalkan.domain.enums import CaseState
from kalkan.ops import auth, labeling
from kalkan.ops.web import current_staff, render, transaction

router = APIRouter(prefix="/cases")

LIMIT = 100
Staff = Annotated[auth.Staff, Depends(current_staff)]


@router.get("")
def search(
    request: Request,
    _: Staff,
    q: str = "",
    customer: str = "",
    state: str = "",
) -> Response:
    text = q.strip()
    query = (
        select(Case, Domain.name, Customer.name, Brand.name, Label.category)
        .join(Domain, Domain.id == Case.domain_id)
        .join(Customer, Customer.id == Case.customer_id)
        .join(Brand, Brand.id == Case.brand_id)
        .outerjoin(Label, Label.case_id == Case.id)
        .order_by(Case.first_seen_at.desc())
        .limit(LIMIT)
    )
    if text:
        pattern = f"%{text.lower().replace('%', '').replace('_', '')}%"
        query = query.where(or_(Domain.name.ilike(pattern), Case.number.ilike(pattern)))
    if customer:
        query = query.where(Customer.slug == customer)
    if state in {item.value for item in CaseState}:
        query = query.where(Case.state == CaseState(state))
    with transaction(request) as session:
        rows = session.execute(query).all()
        customers = session.execute(select(Customer.slug, Customer.name).order_by(Customer.name))
        options = list(customers)
    return render(
        request,
        "cases.html",
        {
            "rows": rows,
            "q": text,
            "customer": customer,
            "state": state,
            "states": list(CaseState),
            "customers": options,
            "limit": LIMIT,
        },
    )


@router.get("/{case_id}")
def detail(request: Request, case_id: uuid.UUID, _: Staff) -> Response:
    with transaction(request) as session:
        case = session.get(Case, case_id)
        if case is None:
            raise HTTPException(status_code=404)
        scans = list(
            session.scalars(
                select(Scan).where(Scan.case_id == case_id).order_by(Scan.started_at.desc())
            )
        )
        artifacts: dict[uuid.UUID | None, list[Artifact]] = {}
        for artifact in session.scalars(
            select(Artifact).where(Artifact.case_id == case_id).order_by(Artifact.created_at)
        ):
            artifacts.setdefault(artifact.scan_id, []).append(artifact)
        events = list(
            session.scalars(
                select(CaseEvent).where(CaseEvent.case_id == case_id).order_by(CaseEvent.at)
            )
        )
        view = labeling.view(session, case)
        context = {
            "case": case,
            "view": view,
            "scans": scans,
            "artifacts": artifacts,
            "events": events,
            "signals": labeling.case_signals(session, case_id),
            "keys": labeling.KEYS,
        }
    return render(request, "case.html", context)
