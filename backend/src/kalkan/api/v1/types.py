"""Small request types shared by the customer API."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field

from kalkan.domain.emails import clean_email


def _address(value: str) -> str:
    cleaned = clean_email(value)
    if cleaned is None:
        raise ValueError("not an e-mail address")
    return cleaned


EmailAddress = Annotated[str, Field(max_length=254), AfterValidator(_address)]
