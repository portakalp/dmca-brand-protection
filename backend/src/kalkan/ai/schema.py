"""The structured output of a classification (architecture.md §13).

The JSON schema sent as ``output_config.format`` is generated from :class:`Verdict` by the SDK
(``anthropic.transform_schema``, which also moves the ``confidence`` bounds into the field's
description). The answer is validated here again: enum values are compared case-insensitively,
``confidence`` must lie in 0-1 and every list is bounded.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _lower_all(value: Any) -> Any:
    return [_lower(item) for item in value] if isinstance(value, list) else value


CategoryName = Literal[
    "phishing_clone",
    "brand_impersonation",
    "affiliate_or_review",
    "parked_or_empty",
    "unrelated",
    "unclear",
]
CopiedElement = Literal["logo", "layout", "text", "favicon", "colors", "brand_name_in_domain"]
Basis = Literal["phishing", "trademark", "none"]

MAX_EVIDENCE = 12
MAX_EVIDENCE_TEXT = 500
MAX_EXPLANATION = 1000


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Annotated[CategoryName, BeforeValidator(_lower)]
    confidence: float = Field(ge=0, le=1)
    copied_elements: Annotated[list[CopiedElement], BeforeValidator(_lower_all)]
    collects_credentials_or_payment: bool
    evidence: list[Annotated[str, Field(max_length=MAX_EVIDENCE_TEXT)]] = Field(
        max_length=MAX_EVIDENCE
    )
    suggested_basis: Annotated[Basis, BeforeValidator(_lower)]
    short_explanation: str = Field(max_length=MAX_EXPLANATION)
