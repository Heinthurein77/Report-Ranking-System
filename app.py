"""
app.py
------
BU Monthly Performance & Ranking System -- main Streamlit entrypoint.

BU users upload a report FILE (any format, unparsed) and see only their
own rank -- no metric entry, no visibility into other BUs' data. Rank is
arrival order within the month, admin-editable afterward. Admin can view/
download every submitted file and override rank/status.

Architecture (see the other modules for the "why"):
    supabase_client.py  Two Supabase clients: per-user session (RLS-scoped)
                         and service-role (self-registration / storage).
    auth.py              Supabase Auth sign-in/out, session + profile state.
    db.py                Data access layer (business_units, profiles,
                         monthly_reports, Supabase Storage uploads).
    ranking.py            Pure deadline/ranking functions (pandas).
    styles.py             Sky Blue light theme CSS + metric-card/status-badge HTML.

Required Supabase setup: run schema.sql once (creates tables + RLS), then
seed one admin profile as described at the bottom of that file.
"""

from datetime import date

import pandas as pd
import streamlit as st

import auth
import db
import ranking
from styles import inject_custom_css, metric_card_html, status_badge_html

st.set_page_config(
    page_title="BU Performance & Ranking System",
    page_icon="\U0001F4CA",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================================
# Login page
# ============================================================================
def render_login() -> None:
    st.markdown(
        """
        <div style="max-width:420px;margin:6vh auto 0 auto;">
            <h2 style="text-align:center;">\U0001F4CA BU Performance &amp; Ranking</h2>
            <p style="text-align:center;color:#475569;">Sign in, or register a new Business Unit account</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        login_tab, register_tab = st.tabs(["Log in", "Register"])

        with login_tab:
            with st.form("login_form"):
                email = st.text_input("Email")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Log in", use_container_width=True)

            if submitted:
                if not email or not password:
                    st.error("Please enter both email and password.")
                else:
                    with st.spinner("Signing in..."):
                        profile = auth.sign_in(email.strip(), password)
                    if profile is None:
                        st.error("Invalid email or password.")
                    elif profile.get("status") == "pending":
                        auth.sign_out()
                        st.warning("Your registration is awaiting admin approval. Please check back later.")
                    elif profile.get("status") == "rejected":
                        auth.sign_out()
                        st.error("Your registration was rejected. Please contact your admin.")
                    elif profile.get("status") != "approved":
                        # Missing/unexpected status -- most likely the `status`
                        # column migration in schema.sql hasn't been applied yet.
                        auth.sign_out()
                        st.error(
                            "Your account is missing a valid status. If you're the admin, make sure the "
                            "`status` column migration in schema.sql has been applied to your database."
                        )
                    else:
                        st.session_state["profile"] = profile
                        st.rerun()

        with register_tab:
            st.caption("New accounts require admin approval before you can log in.")

            existing_bus = db.get_business_units_public()
            if not existing_bus.empty:
                st.caption("Existing Business Units: " + ", ".join(existing_bus["bu_name"]) + " — type one of these exactly to join it, or a new name to create it.")

            with st.form("register_form", clear_on_submit=True):
                reg_full_name = st.text_input("Full name")
                reg_bu_name = st.text_input("Your Business Unit name", placeholder="e.g. Sales")
                reg_email = st.text_input("Email")
                reg_password = st.text_input("Choose a password", type="password")
                reg_password_confirm = st.text_input("Confirm password", type="password")
                reg_submitted = st.form_submit_button("Request Access", use_container_width=True)

            if reg_submitted:
                if not reg_full_name or not reg_bu_name or not reg_email or not reg_password:
                    st.error("Please fill in all fields.")
                elif len(reg_password) < 8:
                    st.error("Password must be at least 8 characters.")
                elif reg_password != reg_password_confirm:
                    st.error("Passwords do not match.")
                else:
                    try:
                        db.self_register(reg_email.strip(), reg_password, reg_full_name.strip(), reg_bu_name.strip())
                        st.success("Registration submitted! An admin must approve your account before you can log in.")
                    except Exception as exc:
                        st.error(f"Registration failed: {exc}")


# ============================================================================
# Shared bits
# ============================================================================
def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### \U0001F464 Account")
        st.caption(st.session_state.get("auth_email", ""))
        st.caption(f"Role: {st.session_state['profile'].get('role')}")
        st.divider()
        if st.button("\U0001F504 Refresh", use_container_width=True):
            st.rerun()
        if st.button("Log out", use_container_width=True):
            auth.sign_out()
            st.rerun()


def render_dashboard_header(title: str) -> None:
    """Title + an explicit refresh button inside the dashboard body itself
    (in addition to the one already in the sidebar), so re-fetching the
    latest data doesn't require hunting for it."""
    col_title, col_refresh = st.columns([5, 1])
    with col_title:
        st.markdown(f"## {title}")
    with col_refresh:
        st.write("")
        if st.button("\U0001F504 Refresh", key=f"refresh_{title}", use_container_width=True):
            st.rerun()


def render_deadline_countdown_value(month_year: str) -> tuple:
    """Returns (value_text, variant) for the deadline KPI card."""
    now = ranking.now_mmt()
    deadline = ranking.get_deadline(month_year)
    if now > deadline:
        return "Deadline passed", "danger"
    days_left = (deadline.date() - now.date()).days
    variant = "warn" if days_left <= 3 else "accent"
    return f"{days_left} day(s) left", variant


# ============================================================================
# BU user dashboard
# ============================================================================
MAX_UPLOAD_MB = 15


def render_bu_dashboard() -> None:
    profile = st.session_state["profile"]
    bu_id = profile.get("bu_id")

    if not bu_id:
        st.error("Your account isn't linked to a Business Unit yet. Please contact your admin.")
        return

    all_bus_df = db.get_business_units()
    bu_row = all_bus_df[all_bus_df["id"] == bu_id]
    bu_name = bu_row.iloc[0]["bu_name"] if not bu_row.empty else "your BU"

    month_year = date.today().strftime("%Y-%m")
    month_label = date.today().strftime("%B %Y")

    render_dashboard_header(f"\U0001F4E4 Submit {month_label} Report")

    countdown_value, countdown_variant = render_deadline_countdown_value(month_year)
    st.markdown(metric_card_html("Deadline (14th, MMT)", countdown_value, "", countdown_variant), unsafe_allow_html=True)
    st.write("")

    existing = db.get_report_for_bu_month(bu_id, month_year)

    if existing:
        st.success(f"Your {month_label} report has been submitted.")
        st.markdown(
            metric_card_html("Your Rank", f"#{existing['rank']}" if existing.get("rank") else "—"),
            unsafe_allow_html=True,
        )
        st.write("")
        st.markdown(f"**File:** {existing['file_name']}")
        st.markdown(status_badge_html(existing["status"]), unsafe_allow_html=True)
        st.markdown(f"[\U0001F4E5 View / download your file]({existing['file_url']})")
        st.caption("Need a correction? Ask your admin -- only admin can edit a submitted report.")
        return

    st.info("Upload your report file below -- any format is accepted as-is, nothing is parsed or validated.")

    uploaded_file = st.file_uploader("Upload report file", type=None)

    if uploaded_file is not None:
        size_mb = uploaded_file.size / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            st.error(f"File is {size_mb:.1f} MB, which exceeds the {MAX_UPLOAD_MB} MB limit.")
        else:
            st.caption(f"Ready to submit: **{uploaded_file.name}** ({size_mb:.2f} MB)")
            if st.button("Submit Report", use_container_width=True):
                try:
                    status = ranking.determine_status(ranking.now_mmt(), month_year)
                    with st.spinner("Uploading..."):
                        db.insert_report(
                            bu_id, bu_name, month_year, uploaded_file, status, st.session_state["auth_user_id"]
                        )
                    st.success(f"Report submitted! Status: {status}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Submission failed: {exc}")


# ============================================================================
# Admin dashboard
# ============================================================================
def render_admin_kpis(all_bus_df: pd.DataFrame, reports_df: pd.DataFrame, month_year: str) -> None:
    total_bus = len(all_bus_df)
    submitted_count = len(reports_df)
    countdown_value, countdown_variant = render_deadline_countdown_value(month_year)

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(metric_card_html("Total Business Units", str(total_bus)), unsafe_allow_html=True)
    with col2:
        st.markdown(
            metric_card_html("Submissions", f"{submitted_count} / {total_bus}", "", "accent"),
            unsafe_allow_html=True,
        )
    with col3:
        st.markdown(
            metric_card_html("Deadline (14th, MMT)", countdown_value, "", countdown_variant), unsafe_allow_html=True
        )


def render_ranking_tab(all_bus_df: pd.DataFrame, month_year: str) -> None:
    reports_df = db.get_reports_for_month(month_year)
    render_admin_kpis(all_bus_df, reports_df, month_year)
    st.write("")

    ranked_df = ranking.compute_rankings(all_bus_df, reports_df)

    display_df = ranked_df[["bu_name", "bu_code", "rank", "status", "file_name"]].copy()
    display_df["rank"] = display_df["rank"].apply(lambda r: int(r) if pd.notna(r) else None)

    st.markdown("#### \U0001F3C6 Current Ranking")
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    submitted_df = ranked_df[ranked_df["id"].notna()]
    if not submitted_df.empty:
        with st.expander("View / download submitted files"):
            for _, row in submitted_df.iterrows():
                st.markdown(f"- **{row['bu_name']}** — [{row['file_name']}]({row['file_url']})")

    st.markdown("#### \U0001F527 Admin Control Panel — Override Rank &amp; Status")
    st.caption("Edit rank or status for any BU below, then Save Changes.")

    editable_df = submitted_df[["id", "bu_name", "rank", "status"]].copy()
    # A report can end up with no rank (e.g. legacy data from before ranks
    # were assigned at insert time) -- .astype(int) crashes on NaN, so any
    # missing rank is pushed to the bottom instead of erroring the whole tab.
    fallback_rank = int(editable_df["rank"].max()) + 1 if editable_df["rank"].notna().any() else 1
    editable_df["rank"] = editable_df["rank"].fillna(fallback_rank).astype(int)
    edited_df = st.data_editor(
        editable_df,
        column_config={
            "id": None,  # hide the raw id column but keep it in the dataframe for updates
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "rank": st.column_config.NumberColumn("Rank", min_value=1, step=1),
            "status": st.column_config.SelectboxColumn("Status", options=["Submitted", "Late", "Pending"]),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key=f"admin_editor_{month_year}",
    )

    if st.button("\U0001F504 Save Ranking Changes", use_container_width=True):
        try:
            for i in range(len(editable_df)):
                report_id = editable_df.iloc[i]["id"]
                new_rank = int(edited_df.iloc[i]["rank"])
                new_status = edited_df.iloc[i]["status"]
                db.update_report_rank_status(report_id, new_rank, new_status)
            st.success("Ranking changes saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not save changes: {exc}")

    st.markdown("#### \U0001F4E4 Export")
    csv_bytes = display_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Ranking (CSV)",
        data=csv_bytes,
        file_name=f"ranking_{month_year}.csv",
        mime="text/csv",
        use_container_width=True,
    )


def render_pending_approvals(all_bus_df: pd.DataFrame) -> None:
    st.markdown("#### ✅ Pending User Approvals")
    pending_df = db.get_pending_profiles()

    if pending_df.empty:
        st.caption("No pending registrations.")
        return

    bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}

    for _, row in pending_df.iterrows():
        col_info, col_approve, col_reject = st.columns([3, 1, 1])
        with col_info:
            bu_name = bu_lookup.get(row["bu_id"], "—")
            st.markdown(f"**{row['full_name']}** — {bu_name}  \n<span style='color:#475569;font-size:0.8rem;'>Requested {row['created_at']}</span>", unsafe_allow_html=True)
        with col_approve:
            if st.button("Approve", key=f"approve_{row['id']}", use_container_width=True):
                db.update_profile_status(row["id"], "approved")
                st.rerun()
        with col_reject:
            if st.button("Reject", key=f"reject_{row['id']}", use_container_width=True):
                db.update_profile_status(row["id"], "rejected")
                st.rerun()


def render_role_management_tab(all_bus_df: pd.DataFrame) -> None:
    render_pending_approvals(all_bus_df)
    st.divider()

    st.markdown("#### \U0001F3E2 Business Units")
    st.caption("Business Units are created by users at registration, not by admin.")
    st.dataframe(all_bus_df, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("#### \U0001F465 User Control")
    st.caption("Change a user's role or status, then Apply Changes. Admin does not create accounts here -- only approves (above) and manages existing ones.")

    profiles_df = db.get_all_profiles()
    bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}

    if profiles_df.empty:
        st.caption("No user accounts yet.")
        return

    profiles_df["bu_name"] = profiles_df["bu_id"].map(bu_lookup)
    editable_df = profiles_df[["full_name", "bu_name", "role", "status", "created_at"]].copy()

    edited_df = st.data_editor(
        editable_df,
        column_config={
            "full_name": st.column_config.TextColumn("Name", disabled=True),
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "role": st.column_config.SelectboxColumn("Role", options=["bu_user", "admin"]),
            "status": st.column_config.SelectboxColumn("Status", options=["pending", "approved", "rejected"]),
            "created_at": st.column_config.TextColumn("Registered At", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="user_control_editor",
    )

    if st.button("Apply Changes", key="apply_user_control", use_container_width=True):
        try:
            db.apply_profile_changes(profiles_df, edited_df)
            st.success("User accounts updated.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not apply changes: {exc}")


def render_admin_dashboard() -> None:
    render_dashboard_header("\U0001F4CA Admin Dashboard")

    all_bus_df = db.get_business_units()
    months = db.get_available_months()
    current_month = date.today().strftime("%Y-%m")
    month_options = sorted(set(months) | {current_month}, reverse=True)
    selected_month = st.selectbox("Reporting period", month_options, index=0)

    tab_ranking, tab_roles = st.tabs(["\U0001F3C6 Ranking", "\U0001F465 Role Management"])
    with tab_ranking:
        render_ranking_tab(all_bus_df, selected_month)
    with tab_roles:
        render_role_management_tab(all_bus_df)


# ============================================================================
# Main
# ============================================================================
def main() -> None:
    inject_custom_css()
    auth.init_session_state()

    if not auth.is_authenticated():
        render_login()
        return

    render_sidebar()

    if auth.is_admin():
        render_admin_dashboard()
    else:
        render_bu_dashboard()


if __name__ == "__main__":
    main()
