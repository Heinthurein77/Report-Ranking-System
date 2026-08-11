"""
supabase_client.py
-------------------
Two different Supabase clients are needed in this app, because Row Level
Security only means something if most requests go through it:

- get_session_client(): built with the ANON/publishable key, then
  authenticated as the CURRENTLY LOGGED-IN USER via their session JWT
  (client.auth.set_session). Every normal read/write in the app goes
  through this client, so RLS policies (in schema.sql) are what actually
  decide what a BU user vs an admin can see or change -- not application
  code. Rebuilt fresh each call since it's per-user, not shared.

- get_service_client(): built with the SERVICE/secret key, which bypasses
  RLS entirely. Used ONLY for admin-provisioning actions that Supabase Auth
  requires elevated privilege for (creating a new login via
  auth.admin.create_user) -- never for regular data reads/writes.
"""

import streamlit as st
from supabase import create_client, Client


def _get_secret(name: str) -> str:
    try:
        return st.secrets[name]
    except KeyError:
        st.error(f"Missing `{name}` in st.secrets. Please configure it in secrets.toml.")
        st.stop()


@st.cache_resource(show_spinner=False)
def get_service_client() -> Client:
    """Service-role client. Bypasses RLS -- admin-provisioning use only."""
    url = _get_secret("SUPABASE_URL")
    key = _get_secret("SUPABASE_SERVICE_KEY")
    return create_client(url, key)


def get_session_client() -> Client:
    """Anon-key client, authenticated as the logged-in user (if any) so
    every query is subject to RLS as that user. Not cached with
    st.cache_resource -- it's cheap to construct and must reflect whichever
    user is logged in in *this* browser session.

    client.auth.set_session() makes a real network call to refresh the
    access token, and Supabase ROTATES the refresh token on every use --
    the one just used becomes invalid. A single page render calls this
    function many times (once per db.py query), so without saving the
    newly-issued pair back to session_state, the second call in the same
    render reuses the now-stale refresh token and fails with an
    "Invalid Refresh Token" AuthApiError."""
    url = _get_secret("SUPABASE_URL")
    anon_key = _get_secret("SUPABASE_ANON_KEY")
    client = create_client(url, anon_key)

    session = st.session_state.get("supabase_session")
    if session:
        try:
            result = client.auth.set_session(session["access_token"], session["refresh_token"])
        except Exception:
            # Genuinely expired/invalid (not just stale from rotation) --
            # clear the dead session and bounce back to a clean login
            # instead of every subsequent query crashing with a confusing
            # auth error.
            for key in ("supabase_session", "auth_user_id", "auth_email", "profile"):
                st.session_state.pop(key, None)
            st.rerun()
        else:
            if result and result.session:
                st.session_state["supabase_session"] = {
                    "access_token": result.session.access_token,
                    "refresh_token": result.session.refresh_token,
                }

    return client
