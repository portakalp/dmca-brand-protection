"""Object paths (architecture.md §11). ``ts`` is ``yyyymmddThhmmssZ``.

Bucket per prefix (K44): ``cases/``, ``messages/``, ``brands/`` and ``customers/`` go to
the evidence bucket; reports, evaluation output and ``demo/`` go to the reports bucket.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from kalkan.domain.enums import ArtifactKind, Profile


def timestamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def case_artifact(
    case_id: uuid.UUID,
    scan_id: uuid.UUID,
    kind: ArtifactKind,
    profile: Profile | None,
    extension: str,
) -> str:
    name = f"{kind.value}-{profile.value}" if profile else kind.value
    return f"cases/{case_id}/{scan_id}/{name}.{extension}"


def case_rdap(case_id: uuid.UUID, at: datetime) -> str:
    return f"cases/{case_id}/rdap/{timestamp(at)}.json"


def case_dns(case_id: uuid.UUID, at: datetime) -> str:
    return f"cases/{case_id}/dns/{timestamp(at)}.json"


def brand_reference(brand_id: uuid.UUID, at: datetime, profile: Profile) -> str:
    return f"brands/{brand_id}/refs/{timestamp(at)}/{profile.value}.png"


def brand_asset(brand_id: uuid.UUID, asset_id: uuid.UUID, extension: str) -> str:
    return f"brands/{brand_id}/assets/{asset_id}.{extension}"


def message_raw(message_id: uuid.UUID) -> str:
    return f"messages/{message_id}.eml"


def customer_loa(customer_id: uuid.UUID, at: datetime, filename: str) -> str:
    return f"customers/{customer_id}/loa/{timestamp(at)}-{filename}"


def demo(customer_id: uuid.UUID, path: str) -> str:
    """Demo images live in the reports bucket because evidence is retained (§11)."""
    return f"demo/{customer_id}/{path}"


def report_file(customer_id: uuid.UUID, period: str, extension: str) -> str:
    """A monthly report's PDF or CSV, in the reports bucket (§21)."""
    return f"reports/{customer_id}/{period}/report-{period}.{extension}"
