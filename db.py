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

# pd.DataFrame([]) has zero COLUMNS, not just zero rows -- which breaks any
# downstream .merge()/column access once a query legitimately returns no
# rows (e.g. no reports yet for the selected month). These are the real
# table columns, used to keep an empty result properly shaped.
BUSINESS_UNIT_COLUMNS = ["id", "bu_name", "bu_code", "created_at"]
REPORT_COLUMNS = [
    "id", "bu_id", "month_year", "metric_1", "metric_2", "total_score",
    "rank", "submitted_at", "status", "submitted_by",
]
PROFILE_COLUMNS = ["id", "full_name", "role", "bu_id", "status", "created_at"]


def _to_df(rows: list, columns: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


# ============================================================================
# business_units
# ============================================================================
def get_business_units() -> pd.DataFrame:
    client = get_session_client()
    resp = client.table("business_units").select("*").order("bu_name").execute()
    return _to_df(resp.data, BUSINESS_UNIT_COLUMNS)


def get_business_units_public() -> pd.DataFrame:
    """For the registration form's BU dropdown, shown BEFORE the visitor is
    signed in -- an anonymous Postgrest role can't satisfy the
    `bu_select_authenticated` RLS policy, so this deliberately uses the
    service client for this one public-facing read-only list."""
    service_client = get_service_client()
    resp = service_client.table("business_units").select("*").order("bu_name").execute()
    return _to_df(resp.data, BUSINESS_UNIT_COLUMNS)


def create_business_unit(bu_name: str, bu_code: str) -> None:
    client = get_session_client()
    client.table("business_units").insert({"bu_name": bu_name, "bu_code": bu_code}).execute()


# ============================================================================
# profiles / role management
# ============================================================================
def get_all_profiles() -> pd.DataFrame:
    client = get_session_client()
    resp = (
        client.table("profiles")
        .select("id, full_name, role, bu_id, status, created_at")
        .order("created_at")
        .execute()
    )
    return _to_df(resp.data, PROFILE_COLUMNS)


def get_pending_profiles() -> pd.DataFrame:
    client = get_session_client()
    resp = (
        client.table("profiles")
        .select("id, full_name, role, bu_id, status, created_at")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return _to_df(resp.data, PROFILE_COLUMNS)


def update_profile_status(profile_id: str, status: str) -> None:
    """Admin approve/reject -- RLS's profiles_update_admin policy is what
    actually restricts this to an admin's own authenticated session."""
    client = get_session_client()
    client.table("profiles").update({"status": status}).eq("id", profile_id).execute()


def update_profile_role_bu(profile_id: str, role: str, bu_id: Optional[str]) -> None:
    client = get_session_client()
    client.table("profiles").update({"role": role, "bu_id": bu_id}).eq("id", profile_id).execute()


def create_bu_user_account(email: str, password: str, full_name: str, role: str, bu_id: Optional[str]) -> dict:
    """Admin-only provisioning: creates the Supabase Auth login (requires
    the service-role client -- this is the one privileged action regular
    RLS can't grant) and the matching profiles row, pre-approved since an
    admin is vouching for it directly. Returns the new profile dict."""
    service_client = get_service_client()

    auth_result = service_client.auth.admin.create_user(
        {
            "email": email,
            "password": password,
            "email_confirm": True,  # internal tool: skip the email confirmation step
        }
    )
    new_user_id = auth_result.user.id

    record = {"id": new_user_id, "full_name": full_name, "role": role, "bu_id": bu_id, "status": "approved"}
    service_client.table("profiles").insert(record).execute()
    return record


def self_register(email: str, password: str, full_name: str, bu_id: str) -> dict:
    """Public self-registration: the login itself is created via the
    ordinary (public) sign-up endpoint on the anon-key client -- no
    service key needed for that part, anyone can call it. The matching
    profiles row is then created via the SERVICE client, deliberately
    bypassing RLS, so role/status can be hard-locked here to
    'bu_user'/'pending' regardless of what a request might try to send --
    a self-registering visitor can never grant themselves admin or
    pre-approve their own account this way."""
    anon_client = get_session_client()
    auth_result = anon_client.auth.sign_up({"email": email, "password": password})
    if not auth_result.user:
        raise RuntimeError("Sign-up did not return a user -- check Supabase Auth settings.")
    new_user_id = auth_result.user.id

    service_client = get_service_client()
    record = {
        "id": new_user_id,
        "full_name": full_name,
        "role": "bu_user",
        "bu_id": bu_id,
        "status": "pending",
    }
    service_client.table("profiles").insert(record).execute()

    # Don't leave the visitor holding a live session for an account that
    # isn't approved yet.
    try:
        anon_client.auth.sign_out()
    except Exception:
        pass

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
    return _to_df(resp.data, REPORT_COLUMNS)


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
