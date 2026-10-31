"""Numbers and rows from the approved panel design (K49).

The design's "today" is 2026-10-08 in Europe/Warsaw; the seed moves every date so that the
design's today is the day it runs. Day index 0 is the oldest day of the 30-day period and
index 29 is today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from kalkan.domain.enums import CaseState, Category, Channel

TIMEZONE = "Europe/Warsaw"
PERIOD_DAYS = 30
# The latest event in the design happens at 10:05 local time on "today".
REFERENCE_TIME = time(10, 30)

# Overview chart, Sep 9 - Oct 8 (sums: 128 detected, 39 taken down).
DETECTED_PER_DAY = (
    3, 5, 4, 2, 6, 4, 3, 5, 7, 4, 3, 2, 4, 6, 5, 3, 4, 8, 5, 4, 3, 6, 4, 5, 3, 2, 4, 5, 6, 3,
)  # fmt: skip
TAKEN_DOWN_PER_DAY = (
    0, 1, 1, 0, 2, 1, 1, 1, 2, 1, 1, 1, 1, 2, 1, 1, 2, 3, 1, 1, 1, 2, 1, 2, 1, 1, 2, 1, 2, 2,
)  # fmt: skip

# Panel status counts in the current period.
ACTIVE = 2
NOTICE_SENT = 6
TAKEN_DOWN = 39
WATCHING = 69
DISMISSED = 12

# Takedown durations in hours, per channel of the last notice before the site went down.
# Medians: Cloudflare 22 h, hosting 34 h, registrar 51 h; overall (first notice to offline,
# registrar cases include the 48 h wait) 31 h.
CLOUDFLARE_HOURS = (6, 9, 11, 13, 15, 17, 19, 20, 22, 24, 26, 28, 29, 30, 33, 38, 44)
HOSTING_HOURS = (14, 18, 23, 27, 31, 31, 34, 34, 37, 40, 45, 52, 60, 70)
REGISTRAR_HOURS = (6, 44, 48, 50, 52, 55, 60, 70)
ESCALATION_HOURS = 48

# Previous 30 days: 108 detected, 43 verified (all taken down by now), median 39.75 h, so the
# KPI deltas read +19 %, +9 % and "22 % faster" (product.md §12 rounding).
PREVIOUS_DETECTED = 108
PREVIOUS_TAKEN_DOWN = 43
PREVIOUS_DISMISSED = 15
PREVIOUS_HOURS = (
    8, 10, 12, 14, 15, 17, 18, 20, 21, 23, 24, 26, 27, 28, 30, 31, 33, 34, 36, 37, 39,
    39.75,
    41, 43, 45, 47, 50, 53, 56, 60, 64, 68, 72, 76, 80, 85, 90, 96, 100, 104, 110, 115, 120,
)  # fmt: skip


@dataclass(frozen=True)
class RecentRow:
    """A row of the overview's "Recent threats" table."""

    domain: str
    category: Category
    state: CaseState
    day: int  # first-seen day index
    at: time  # first-seen local time
    hosting: str | None
    is_cloudflare: bool
    last_action: str


DETAIL_DOMAIN = "mintera-login.com"

RECENT_ROWS = (
    RecentRow(DETAIL_DOMAIN, Category.PHISHING_CLONE, CaseState.NOTIFIED, 29, time(9, 14),
              "Cloudflare", True, "Host notified"),
    RecentRow("mintera-rewards.top", Category.BRAND_IMPERSONATION, CaseState.APPROVED, 29,
              time(7, 52), "OVHcloud", False, "Notice being prepared"),
    RecentRow("mintera-secure.xyz", Category.PHISHING_CLONE, CaseState.NOTIFIED, 26, time(11, 3),
              "Cloudflare", True, "Sent to registrar"),
    RecentRow("minteraa.net", Category.PHISHING_CLONE, CaseState.TAKEN_DOWN, 25, time(10, 12),
              "Hetzner", False, "Confirmed offline"),
    RecentRow("mint-era-app.com", Category.PARKED_OR_EMPTY, CaseState.WATCHING, 24,
              time(16, 40), None, False, "Parked page found"),
    RecentRow("mintera-login2.com", Category.PHISHING_CLONE, CaseState.TAKEN_DOWN, 21,
              time(8, 35), "DigitalOcean", False, "Registrar suspended"),
)  # fmt: skip

# Taken-down rows with fixed timing: (domain, channel, hours from that channel's notice).
FIXED_TAKEDOWNS = {
    "minteraa.net": (Channel.HOSTING, 23),
    "mintera-login2.com": (Channel.REGISTRAR, 6),
}

# Whitelist: 24 entries, 3 of them added this month (Oct 5, 3 and 1 in the design).
WHITELIST_RECENT = (
    ("mintera.pl", "official", 3, "Official site"),
    ("mintera-bank.eu", "mirror", 5, "Alternate domain"),
    ("partners.mintera-referrals.com", "affiliate", 7, "Affiliate"),
)  # (pattern, kind, days ago, note)
WHITELIST_OLDER = (
    ("mintera.com", "official"),
    ("mintera.de", "official"),
    ("mintera-bank.com", "mirror"),
    ("mintera-cards.com", "mirror"),
    ("mintera1.com", "mirror"),
    ("mintera-offers.com", "official"),
    ("mintera-referrals.com", "affiliate"),
    ("bank-reviews-hub.net", "affiliate"),
    ("topsavingsaccounts.org", "affiliate"),
    ("porownaj-konta.pl", "affiliate"),
    ("kredyt-ranking.pl", "affiliate"),
    ("openbanking-partners.com", "partner"),
    ("mintera-payments.com", "partner"),
    ("support.mintera-help.com", "partner"),
    ("status.mintera.io", "partner"),
    ("mintera.substack.com", "social"),
    ("mintera.tumblr.com", "social"),
    ("minteraofficial.medium.com", "social"),
    ("mintera.discord.gg", "social"),
    ("mintera-careers.com", "other"),
    ("mintera-press.com", "other"),
)  # (pattern, kind); added before the 30-day period
