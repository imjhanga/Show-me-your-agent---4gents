"""Pinned clinic clock.

``dental_dataset`` was generated against a fixed reference date, so every run
of the demo evaluates against that same instant rather than the wall clock.
Without this the overdue count drifts daily and the scripted demo stops being
reproducible.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CLINIC_TIMEZONE = "Asia/Singapore"
CLINIC_ZONE = ZoneInfo(CLINIC_TIMEZONE)

# dental_dataset/README.md documents this as the generation reference date.
DATASET_REFERENCE_DATE = date(2026, 9, 8)

# 09:00 sits inside the configured sending window, so a default scan is not
# blocked by CONTACT_TIMING_NOT_PERMITTED for every single case.
DEFAULT_CLINIC_NOW = datetime(
    DATASET_REFERENCE_DATE.year,
    DATASET_REFERENCE_DATE.month,
    DATASET_REFERENCE_DATE.day,
    9,
    0,
    tzinfo=CLINIC_ZONE,
)


def parse_clinic_now(value: str | None) -> datetime:
    """Parse a ``--clinic-now`` argument, defaulting to the dataset date."""

    if value is None:
        return DEFAULT_CLINIC_NOW
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CLINIC_ZONE)
    return parsed


def clinic_today(clinic_now: datetime) -> date:
    return clinic_now.astimezone(CLINIC_ZONE).date()


def at_clinic_hour(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=CLINIC_ZONE)


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def add_days(moment: datetime, days: int) -> datetime:
    return moment + timedelta(days=days)
