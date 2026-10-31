"""Global settings (architecture.md §14, §15): thresholds, switches, channel limits.

Switches need the confirmation box. Turning the global send switch on leaves a
``clear_blocked_cases`` job for every customer (§7). ``NOTICES_DRY_RUN`` and
``DRY_RUN_REDIRECT_EMAIL`` are environment variables and only shown.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import select

from kalkan.db.models import AuditLog, Setting
from kalkan.db.outbox import CLEAR_BLOCKED_CASES, queue_job
from kalkan.db.settings_store import SPECS, SettingError, read_settings, write_setting
from kalkan.ops import auth
from kalkan.ops.web import (
    client_ip,
    csrf_guard,
    current_staff,
    now,
    redirect,
    render,
    services,
    transaction,
)

router = APIRouter(prefix="/settings")
CONFIRM = "yes"
LIMIT_PREFIX = "limit_"


@router.get("")
def settings_page(request: Request, _: Annotated[auth.Staff, Depends(current_staff)]) -> Response:
    with transaction(request) as session:
        values = read_settings(session)
        rows = {row.key: row for row in session.scalars(select(Setting))}
    env = services(request).settings
    return render(
        request,
        "settings.html",
        {
            "specs": SPECS,
            "values": values,
            "rows": rows,
            "notices_dry_run": env.notices_dry_run,
            "dry_run_redirect_email": env.dry_run_redirect_email,
        },
    )


@router.post("/recheck-blocked")
def recheck_blocked(
    request: Request,
    staff: Annotated[auth.Staff, Depends(csrf_guard)],
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """After the lawyer's approval of a legal text (deployed) or any other lifted gate (K137)."""
    if confirm != CONFIRM:
        return redirect("/settings", "Error: tick the confirmation box to re-check.")
    with transaction(request) as session:
        queue_job(session, CLEAR_BLOCKED_CASES)
        session.add(
            AuditLog(
                actor=staff.email,
                action="notices.recheck_blocked",
                target="all",
                data={},
                ip=client_ip(request),
                at=now(),
            )
        )
    return redirect("/settings", "Blocked cases will be planned again.")


@router.post("/{key}")
async def change(
    request: Request, key: str, staff: Annotated[auth.Staff, Depends(csrf_guard)]
) -> Response:
    spec = SPECS.get(key)
    if spec is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    raw: object
    if spec.kind == "limits":
        raw = {
            name.removeprefix(LIMIT_PREFIX): value
            for name, value in form.multi_items()
            if name.startswith(LIMIT_PREFIX)
        }
    else:
        raw = form.get("value", "")
    if spec.kind == "switch" and form.get("confirm") != CONFIRM:
        return redirect("/settings", "Error: tick the confirmation box to change a switch.")
    return await run_in_threadpool(_apply, request, key, raw, staff)


def _apply(request: Request, key: str, raw: object, staff: auth.Staff) -> Response:
    with transaction(request) as session:
        try:
            changed = write_setting(session, key, raw, actor=staff.email)
        except SettingError as exc:
            return redirect("/settings", f"Error: {exc}")
        if changed is None:
            return redirect("/settings", f"{key} already had that value.")
        if changed.lifts_block:
            queue_job(session, CLEAR_BLOCKED_CASES)
        session.add(
            AuditLog(
                actor=staff.email,
                action=f"setting.{key}",
                target=key,
                data={"old": changed.old, "new": changed.new},
                ip=client_ip(request),
                at=now(),
            )
        )
    return redirect("/settings", f"{key}: {changed.old} → {changed.new}")
