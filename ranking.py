"""
ranking.py
----------
Pure functions for scoring, deadline/lateness, and ranking. Kept free of
any Streamlit or Supabase calls so this logic can be unit-tested in
isolation and reasoned about without the rest of the app.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

# Myanmar Standard Time -- fixed offset, no DST.
MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))
DEADLINE_DAY = 14


def now_mmt() -> datetime:
    return datetime.now(MYANMAR_TZ)


def get_deadline(month_year: str) -> datetime:
    """23:59:59 MMT on the 14th of the given 'YYYY-MM' period."""
    year, month = (int(part) for part in month_year.split("-"))
    return datetime(year, month, DEADLINE_DAY, 23, 59, 59, tzinfo=MYANMAR_TZ)


def determine_status(submitted_at: datetime, month_year: str) -> str:
    """'Late' if submitted after 23:59 MMT on the 14th, else 'Submitted'."""
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    return "Late" if submitted_at.astimezone(MYANMAR_TZ) > get_deadline(month_year) else "Submitted"


def compute_total_score(metric_1: float, metric_2: float) -> float:
    """Combined score used for ranking. Simple sum by default -- change
    this one function if the business needs a weighted formula instead."""
    return float(metric_1) + float(metric_2)


def validate_metrics(metric_1: Optional[float], metric_2: Optional[float]) -> list:
    """Returns a list of validation error strings (empty list = valid).
    Rejects missing values and negative numbers, per spec."""
    errors = []
    if metric_1 is None:
        errors.append("Metric 1 is required.")
    elif metric_1 < 0:
        errors.append("Metric 1 cannot be negative.")
    if metric_2 is None:
        errors.append("Metric 2 is required.")
    elif metric_2 < 0:
        errors.append("Metric 2 cannot be negative.")
    return errors


def compute_rankings(all_bus_df: pd.DataFrame, reports_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join the full BU roster onto this month's reports so every BU
    appears even if it hasn't submitted yet (status becomes 'Pending', no
    rank). Submitted BUs are ranked by total_score descending -- a
    "provisional" ranking, since it only reflects whoever has submitted
    so far. Ties share the same rank (method='min', i.e. 1,1,3 not 1,1,2).

    all_bus_df: columns [id, bu_name, bu_code]
    reports_df: columns [bu_id, metric_1, metric_2, total_score, status, submitted_at, ...]
    """
    merged = all_bus_df.rename(columns={"id": "bu_id"}).merge(reports_df, on="bu_id", how="left")
    merged["status"] = merged["status"].fillna("Pending")

    submitted_mask = merged["total_score"].notna()
    merged.loc[submitted_mask, "rank"] = (
        merged.loc[submitted_mask, "total_score"].rank(ascending=False, method="min").astype(int)
    )

    # Ranked BUs first (by rank), Pending BUs after, in a stable BU-name order.
    merged["_sort_key"] = merged["rank"].fillna(float("inf"))
    merged = merged.sort_values(by=["_sort_key", "bu_name"]).drop(columns="_sort_key").reset_index(drop=True)
    return merged
