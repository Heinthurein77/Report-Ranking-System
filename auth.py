"""
auth.py
-------
Authentication against Supabase Auth (not a custom users table). There is
no public self-signup in this version -- accounts are provisioned by an
admin from the Role Management panel (see db.py: create_bu_user_account).

Session handling: Supabase's Python client is stateless across Streamlit
reruns (each rerun is a fresh script execution), so the access/refresh
tokens returned at login are stored in st.session_state and replayed into
a fresh client via client.auth.set_session() on every subsequent call --
see supabase_client.get_session_client().
"""

from typing import Optional

import streamlit as st

from supabase_client import get_session_client


def sign_in(email: str, password: str) -> Optional[dict]:
    """Attempt login; on success, stores the session and returns the
    signed-in user's profile (role, bu_id, ...). Returns None on failure."""
    client = get_session_client()
    try:
        result = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:
        return None

    if not result.session or not result.user:
        return None

    st.session_state["supabase_session"] = {
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
    }
    st.session_state["auth_user_id"] = result.user.id
    st.session_state["auth_email"] = result.user.email

    return fetch_current_profile()


def fetch_current_profile() -> Optional[dict]:
    """Reads the profiles row for the logged-in user, via the session
    client -- RLS's `auth.uid() = id` policy is what allows this to
    succeed for a non-admin reading their own row."""
    user_id = st.session_state.get("auth_user_id")
    if not user_id:
        return None

    client = get_session_client()
    resp = client.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def sign_out() -> None:
    client = get_session_client()
    try:
        client.auth.sign_out()
    except Exception:
        pass
    for key in ["supabase_session", "auth_user_id", "auth_email", "profile"]:
        st.session_state.pop(key, None)


def init_session_state() -> None:
    st.session_state.setdefault("supabase_session", None)
    st.session_state.setdefault("auth_user_id", None)
    st.session_state.setdefault("auth_email", None)
    st.session_state.setdefault("profile", None)


def is_authenticated() -> bool:
    return st.session_state.get("supabase_session") is not None and st.session_state.get("profile") is not None


def is_admin() -> bool:
    profile = st.session_state.get("profile") or {}
    return profile.get("role") == "admin"
