"""Wall-clock helpers.

Containers run in UTC; the family does not. At 03:00 Moscow time a UTC
container still thinks it is yesterday — which is exactly when the store parser
runs, so `date.today()` there would misdate every price observation by a day.

Application code must call these instead of `date.today()` / `datetime.now()`.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def now() -> datetime:
    """Timezone-aware 'now' in the family's timezone."""
    return datetime.now(tz())


def today() -> date:
    """The calendar date as the family experiences it."""
    return now().date()
