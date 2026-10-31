"""Handing work to the workers through ``jobs_outbox`` (architecture.md §7; K3).

A row is written in the same transaction as the change that needs the job; the scheduler's
dispatcher sends it to Dramatiq within seconds (``kalkan.scheduler.outbox``). Payload values
are JSON; ids are strings.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from kalkan.db.models import JobOutbox

WHITELIST_CHANGED = "whitelist_changed"
ENRICH_DOMAIN = "enrich_domain"
REFRESH_BRAND_FAVICONS = "refresh_brand_favicons"
REFRESH_BRAND_REFERENCES = "refresh_brand_references"
HASH_BRAND_ASSETS = "hash_brand_assets"
HTTP_PRECHECK = "http_precheck"
COMPUTE_SIGNALS = "compute_signals"
# A change lifted a sending gate (§7, §15); no customer_id = every customer (kill switch).
CLEAR_BLOCKED_CASES = "clear_blocked_cases"
# The notice engine (§15): plan a case's first wave, write a notice's explanation (worker-ai),
# send a notice, send the customer's phishing alert.
PLAN_NOTICES = "plan_notices"
AI_NOTICE_TEXT = "ai_notice_text"
SEND_NOTICE = "send_notice"
CUSTOMER_ALERT = "customer_alert"
# The follow-up engine (§16): check a case now (a "removed" reply), process a webhook event
# the API stored (the API writes this row in the same transaction).
RECHECK = "recheck"
PROCESS_WEBHOOK_EVENT = "process_webhook_event"
# A monthly report the operator asked for (ops panel, CLI; §21).
BUILD_REPORT = "build_report"
# A reply the ops panel linked to its case goes to worker-ai for classification.
CLASSIFY_REPLY = "ai_classify_reply"


def queue_job(session: Session, kind: str, **payload: Any) -> None:
    session.add(JobOutbox(kind=kind, payload={key: str(value) for key, value in payload.items()}))
