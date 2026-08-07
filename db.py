"""
db.py
-----
Data access layer. Every function here goes through the per-session
authenticated client (supabase_client.get_session_client) UNLESS it's
explicitly a self-registration action that needs to bypass RLS (see
self_register/get_or_create_business_unit) -- see supabase_client.py for
why that split exists. RLS policies (schema.sql) are the real
authorization boundary; this module doesn't re-check roles itself.

There is no admin-side account/BU creation in this version: users create
their own account AND their own Business Unit name at registration time
(self_register); admin's role is limited to approving/rejecting
registrations and controlling existing accounts afterward (role/status).
"""

import re
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
    """For the registration form (shown as a reference list of existing BU
    names, so a new registrant can match spelling instead of accidentally
    creating a near-duplicate), used BEFORE the visitor is signed in -- an
    anonymous Postgrest role can't satisfy the `bu_select_authenticated`
    RLS policy, so this deliberately uses the service client."""
    service_client = get_service_client()
    resp = service_client.table("business_units").select("*").order("bu_name").execute()
    return _to_df(resp.data, BUSINESS_UNIT_COLUMNS)


def _slugify_bu_code(bu_name: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]+", "", bu_name).upper()[:10]
    return code or "BU"


def get_or_create_business_unit(bu_name: str) -> str:
    """Registration lets a user type their own Business Unit name rather
    than pick from an admin-curated list, so this is the one place BUs get
    created. Matches case-insensitively against existing names first, so
    "Sales" and "sales" reuse the same BU instead of fragmenting the
    ranking across near-duplicate rows. Uses the service client since an
    unauthenticated registrant can't satisfy bu_write_admin. Returns the
    BU's id either way."""
    service_client = get_service_client()
    bu_name = bu_name.strip()

    existing = (
        service_client.table("business_units")
        .select("id, bu_name")
        .ilike("bu_name", bu_name)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"]

    base_code = _slugify_bu_code(bu_name)
    code = base_code
    suffix = 1
    while True:
        try:
            created = (
                service_client.table("business_units")
                .insert({"bu_name": bu_name, "bu_code": code})
                .execute()
            )
            return created.data[0]["id"]
        except Exception as exc:
            # bu_code collision (different name, same slug) -- try the next suffix.
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                suffix += 1
                code = f"{base_code}{suffix}"
                if suffix > 50:
                    raise
            else:
                raise


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


def apply_profile_changes(original_df: pd.DataFrame, edited_df: pd.DataFrame) -> None:
    """Admin's ongoing user-control function: diff an edited copy of
    get_all_profiles() against the original and push only the changed
    role/status values. RLS's profiles_update_admin policy is what
    actually restricts this to an admin's own authenticated session."""
    client = get_session_client()
    for i in range(len(original_df)):
        profile_id = original_df.iloc[i]["id"]
        changes = {}
        if edited_df.iloc[i]["role"] != original_df.iloc[i]["role"]:
            changes["role"] = edited_df.iloc[i]["role"]
        if edited_df.iloc[i]["status"] != original_df.iloc[i]["status"]:
            changes["status"] = edited_df.iloc[i]["status"]
        if changes:
            client.table("profiles").update(changes).eq("id", profile_id).execute()


def self_register(email: str, password: str, full_name: str, bu_name: str) -> dict:
    """Public self-registration: the login itself is created via the
    ordinary (public) sign-up endpoint on the anon-key client -- no
    service key needed for that part, anyone can call it. The Business
    Unit is created (or matched to an existing one -- see
    get_or_create_business_unit) from whatever name the registrant types;
    there is no admin-curated BU list to pick from. The matching profiles
    row is then created via the SERVICE client, deliberately bypassing
    RLS, so role/status can be hard-locked here to 'bu_user'/'pending'
    regardless of what a request might try to send -- a self-registering
    visitor can never grant themselves admin or pre-approve their own
    account this way."""
    bu_id = get_or_create_business_unit(bu_name)

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
