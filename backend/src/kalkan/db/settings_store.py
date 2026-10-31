"""The global ``settings`` table (architecture.md §14, §15): keys, types and validation.

The first migration writes every key with its default. The ops panel changes them through
:func:`write_setting`; the decision engine and the sender read them with
:func:`read_settings`. Values are JSON; unknown keys are refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan.db.base import utcnow
from kalkan.db.models import Setting

Kind = Literal["switch", "ratio", "limits"]
MAX_DAILY_LIMIT = 10_000


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: Kind
    default: Any
    help: str
    # Switching it on lifts a sending gate (§7): blocked cases are re-planned.
    lifts_block: bool = False


SPECS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            "sending_enabled",
            "switch",
            False,
            "Global send switch. Off = real sending stopped (kill switch).",
            lifts_block=True,
        ),
        SettingSpec(
            "auto_approve_enabled",
            "switch",
            False,
            "Cases both models confirm are approved without review (§14).",
        ),
        SettingSpec("triage_min_confidence", "ratio", 0.90, "R1: triage confidence."),
        SettingSpec("confirm_min_confidence", "ratio", 0.90, "Confirmation confidence."),
        SettingSpec("visual_similarity_min", "ratio", 0.85, "R1: visual similarity."),
        SettingSpec("dismiss_min_confidence", "ratio", 0.80, "R4: dismissal confidence."),
        SettingSpec(
            "channel_daily_limits",
            "limits",
            {"cloudflare": 50, "hosting": 50, "registrar": 20, "registry": 20, "google": 50},
            "Real notices per channel and UTC day (§15 gate 6).",
        ),
    )
}


class SettingError(ValueError):
    """An invalid setting value; the message is shown to the operator."""


def parse_value(spec: SettingSpec, raw: Any) -> Any:
    """``raw`` is a form value (string) or already typed JSON."""
    if spec.kind == "switch":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"true", "on", "1"}:
            return True
        if text in {"false", "off", "0"}:
            return False
        raise SettingError(f"{spec.key}: expected on or off")
    if spec.kind == "ratio":
        try:
            value = float(str(raw).strip().replace(",", "."))
        except ValueError:
            raise SettingError(f"{spec.key}: expected a number between 0 and 1") from None
        if not 0 <= value <= 1:
            raise SettingError(f"{spec.key}: expected a number between 0 and 1")
        return round(value, 4)
    if not isinstance(raw, Mapping):
        raise SettingError(f"{spec.key}: expected a limit per channel")
    limits: dict[str, int] = {}
    for channel, limit in raw.items():
        try:
            number = int(str(limit).strip())
        except ValueError:
            raise SettingError(f"{spec.key}: the {channel} limit must be a whole number") from None
        if not 0 <= number <= MAX_DAILY_LIMIT:
            raise SettingError(f"{spec.key}: the {channel} limit must be 0-{MAX_DAILY_LIMIT}")
        limits[str(channel)] = number
    return limits


def read_settings(session: Session) -> dict[str, Any]:
    """Every known key; a missing row reads as its default."""
    stored = {row.key: row.value for row in session.scalars(select(Setting))}
    return {key: stored.get(key, spec.default) for key, spec in SPECS.items()}


@dataclass(frozen=True)
class Change:
    key: str
    old: Any
    new: Any

    @property
    def lifts_block(self) -> bool:
        return SPECS[self.key].lifts_block and self.new is True and self.old is not True


def write_setting(session: Session, key: str, raw: Any, *, actor: str) -> Change | None:
    """Validate and store one value; ``None`` when it did not change."""
    spec = SPECS.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")
    value = parse_value(spec, raw)
    row = session.get(Setting, key, with_for_update=True)
    old = row.value if row is not None else spec.default
    if row is not None and old == value:
        return None
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=actor))
    else:
        row.value = value
        row.updated_by = actor
        row.updated_at = utcnow()
    session.flush()
    return Change(key, old, value)
