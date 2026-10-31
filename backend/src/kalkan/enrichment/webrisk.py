"""Google Web Risk Lookup API (architecture.md §9).

``uris.search`` for SOCIAL_ENGINEERING, MALWARE and UNWANTED_SOFTWARE. An empty JSON object
means the URL is on no list. Without ``WEBRISK_API_KEY`` the check is skipped. Web Risk is a
known provider, outside ``net/safe_http.py`` (§10).
"""

from __future__ import annotations

from typing import Any

import httpx2

SEARCH_URL = "https://webrisk.googleapis.com/v1/uris:search"
THREAT_TYPES = ("SOCIAL_ENGINEERING", "MALWARE", "UNWANTED_SOFTWARE")


class WebRiskClient:
    def __init__(self, api_key: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    def lookup(self, host: str) -> dict[str, Any]:
        """``{"threat_types": [...], "expire_time": ...}`` for the host's root URL."""
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("threatTypes", threat) for threat in THREAT_TYPES
        ]
        params.append(("uri", f"http://{host}/"))
        # The key goes in a header, never in the URL, which HTTP clients log.
        headers = {"X-Goog-Api-Key": self._api_key}
        with httpx2.Client(transport=self._transport, timeout=5.0) as client:
            response = client.get(SEARCH_URL, params=params, headers=headers)
        if response.status_code != 200:
            raise WebRiskError(f"Web Risk lookup failed with HTTP {response.status_code}")
        threat = response.json().get("threat") or {}
        return {
            "threat_types": sorted(threat.get("threatTypes") or []),
            "expire_time": threat.get("expireTime"),
        }


class WebRiskError(RuntimeError):
    pass
