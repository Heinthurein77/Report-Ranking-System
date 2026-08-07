"""
ranking.py
----------
Pure functions for deadline/lateness and ranking. Kept free of any
Streamlit or Supabase calls so this logic can be unit-tested in isolation
and reasoned about without the rest of the app.

There's no metric/score data in this version -- a BU just uploads a report
file, unparsed. Rank is arrival order within the month (1st BU to submit
gets rank 1), assigned at insert time (see db.py: get_next_rank) and
editable by admin afterward.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

# Myanmar Standard Time -- fixed offset, no DST.
MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))
DEADLINE_DAY = 14


def now_mmt() -> datetime:
    return datetime.now(MYANMAR_TZ)


def today_mmt() -> date:
    """The reporting period a submission belongs to, and the deadline
    countdown, must follow Myanmar's calendar day -- not the app server's
    (Streamlit Cloud typically runs UTC), which would misfile submissions
    made near the UTC/MMT day boundary."""
    return now_mmt().date()


def parse_to_mmt(timestamp) -> Optional[datetime]:
    """Parse a Supabase timestamptz string (stored in UTC) and convert to
    Myanmar time. Every report/account timestamp in the UI should go
    through this before being shown -- Supabase always returns UTC.

    Deliberately not type-hinted as `str`: this is fed directly from
    pandas columns (e.g. a left-joined "no report yet" row), where a
    missing value shows up as float NaN -- which is truthy in Python, so
    `if not timestamp` alone doesn't catch it, and datetime.fromisoformat
    raises TypeError (not ValueError) on a non-string input."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYANMAR_TZ)


def format_mmt(timestamp, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = parse_to_mmt(timestamp)
    return dt.strftime(fmt) if dt else ""


def get_deadline(month_year: str) -> datetime:
    """23:59:59 MMT on the 14th of the given 'YYYY-MM' period."""
    year, month = (int(part) for part in month_year.split("-"))
    return datetime(year, month, DEADLINE_DAY, 23, 59, 59, tzinfo=MYANMAR_TZ)


def determine_status(submitted_at: datetime, month_year: str) -> str:
    """'Late' if submitted after 23:59 MMT on the 14th, else 'Submitted'."""
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    return "Late" if submitted_at.astimezone(MYANMAR_TZ) > get_deadline(month_year) else "Submitted"


def compute_rankings(all_bus_df: pd.DataFrame, reports_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join the full BU roster onto this month's reports so every BU
    appears even if it hasn't submitted yet (status becomes 'Pending', no
    rank). Submitted BUs are ordered by their stored `rank` (arrival order,
    admin-editable) -- this function doesn't compute rank itself, just
    orders by whatever's already on each row.

    all_bus_df: columns [id, bu_name, bu_code]
    reports_df: columns [bu_id, file_name, file_url, rank, status, submitted_at, ...]
    """
    merged = all_bus_df.rename(columns={"id": "bu_id"}).merge(reports_df, on="bu_id", how="left")
    merged["status"] = merged["status"].fillna("Pending")

    # Ranked BUs first (by rank), Pending BUs after, in a stable BU-name order.
    merged["_sort_key"] = merged["rank"].fillna(float("inf"))
    merged = merged.sort_values(by=["_sort_key", "bu_name"]).drop(columns="_sort_key").reset_index(drop=True)
    return merged
