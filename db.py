"""
db.py
-----
Data access layer. Every function here goes through the per-session
authenticated client (supabase_client.get_session_client) UNLESS it's
explicitly an admin-provisioning action that Supabase Auth requires the
service-role client for (create_bu_user_account) -- see supabase_client.py
for why that split exists. RLS policies (schema.sql) are the real
authorization boundary; this module doesn't re-check roles itself.
"""

from typing import Optional

import pandas as pd

from supabase_client import get_session_client, get_service_client


# ============================================================================
# business_units
# ============================================================================
def get_business_units() -> pd.DataFrame:
    client = get_session_client()
    resp = client.table("business_units").select("*").order("bu_name").execute()
    return pd.DataFrame(resp.data or [])


def create_business_unit(bu_name: str, bu_code: str) -> None:
    client = get_session_client()
    client.table("business_units").insert({"bu_name": bu_name, "bu_code": bu_code}).execute()


# ============================================================================
# profiles / role management
# ============================================================================
def get_all_profiles() -> pd.DataFrame:
    client = get_session_client()
    resp = client.table("profiles").select("id, full_name, role, bu_id, created_at").order("created_at").execute()
    return pd.DataFrame(resp.data or [])


def update_profile_role_bu(profile_id: str, role: str, bu_id: Optional[str]) -> None:
    client = get_session_client()
    client.table("profiles").update({"role": role, "bu_id": bu_id}).eq("id", profile_id).execute()


def create_bu_user_account(email: str, password: str, full_name: str, role: str, bu_id: Optional[str]) -> dict:
    """Admin-only provisioning: creates the Supabase Auth login (requires
    the service-role client -- this is the one privileged action regular
    RLS can't grant) and the matching profiles row. Returns the new
    profile dict."""
    service_client = get_service_client()

    auth_result = service_client.auth.admin.create_user(
        {
            "email": email,
            "password": password,
            "email_confirm": True,  # internal tool: skip the email confirmation step
        }
    )
    new_user_id = auth_result.user.id

    record = {"id": new_user_id, "full_name": full_name, "role": role, "bu_id": bu_id}
    service_client.table("profiles").insert(record).execute()
    return record


# ============================================================================
# monthly_reports
# ============================================================================
def get_report_for_bu_month(bu_id: str, month_year: str) -> Optional[dict]:
    client = get_session_client()
    resp = (
        client.table("monthly_reports")
        .select("*")
        .eq("bu_id", bu_id)
        .eq("month_year", month_year)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def get_reports_for_month(month_year: str) -> pd.DataFrame:
    client = get_session_client()
    resp = client.table("monthly_reports").select("*").eq("month_year", month_year).execute()
    return pd.DataFrame(resp.data or [])


def get_available_months() -> list:
    client = get_session_client()
    resp = client.table("monthly_reports").select("month_year").execute()
    return sorted({row["month_year"] for row in (resp.data or [])}, reverse=True)


def insert_report(bu_id: str, month_year: str, metric_1: float, metric_2: float, total_score: float, status: str, submitted_by: str) -> dict:
    client = get_session_client()
    record = {
        "bu_id": bu_id,
        "month_year": month_year,
        "metric_1": metric_1,
        "metric_2": metric_2,
        "total_score": total_score,
        "status": status,
        "submitted_by": submitted_by,
    }
    resp = client.table("monthly_reports").insert(record).execute()
    return resp.data[0]


def update_report(report_id: str, metric_1: float, metric_2: float, total_score: float, status: str) -> None:
    """Admin correction path -- RLS only allows this via the admin's own
    session (reports_update_admin policy checks is_admin())."""
    client = get_session_client()
    client.table("monthly_reports").update(
        {"metric_1": metric_1, "metric_2": metric_2, "total_score": total_score, "status": status}
    ).eq("id", report_id).execute()
