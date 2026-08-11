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

import altair as alt
import pandas as pd
import streamlit as st

import auth
import db
import ranking
from styles import empty_state_html, inject_custom_css, metric_card_html, section_header_html, status_badge_html

# Colorblind-validated categorical palette, fixed order (never cycled/
# reassigned per filter change) -- see dataviz skill references/palette.md.
TREND_CHART_PALETTE = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
    "#e87ba4", "#008300", "#4a3aa7", "#e34948",
]
MAX_TREND_SERIES = len(TREND_CHART_PALETTE)

RANKING_DISPLAY_COLUMNS = {
    "bu_name": "Business Unit",
    "bu_code": "Code",
    "rank": "Rank",
    "status": "Status",
    "file_name": "File",
    "submitted_at": "Submitted At (MMT)",
}

st.set_page_config(
    page_title="BU Performance & Ranking System",
    page_icon="\U0001F4CA",
    layout="wide",
    initial_sidebar_state="expanded",
)


def flash(message: str, kind: str = "success") -> None:
    """Streamlit clears whatever a script just rendered (including
    st.success/st.error) the instant st.rerun() fires, before the user
    can actually read it -- so every action that reruns right after
    succeeding/failing stores its notification here instead, and
    render_flash() (called once near the top of every page) displays it
    AFTER the rerun completes, where it's actually visible."""
    st.session_state["_flash"] = (kind, message)


def render_flash() -> None:
    flash_data = st.session_state.pop("_flash", None)
    if not flash_data:
        return
    kind, message = flash_data
    getattr(st, kind)(message)


# ============================================================================
# Login page
# ============================================================================
def render_login() -> None:
    render_flash()
    st.markdown('<div class="login-wrapper">', unsafe_allow_html=True)
    st.markdown('<div class="logo-mark">\U0001F4CA</div>', unsafe_allow_html=True)
    st.markdown("<h2>BU Performance &amp; Ranking</h2>", unsafe_allow_html=True)
    st.markdown('<p class="subtitle">Sign in, or register a new Business Unit account</p>', unsafe_allow_html=True)

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
        st.markdown('<p class="footnote">Forgot your password? Contact your admin.</p>', unsafe_allow_html=True)

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

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================================
# Shared bits
# ============================================================================
def render_sidebar() -> None:
    with st.sidebar:
        email = st.session_state.get("auth_email") or ""
        initials = email[:2].upper() if email else "?"
        st.markdown(f'<div class="avatar-circle">{initials}</div>', unsafe_allow_html=True)
        st.markdown("**Account**")
        st.caption(email)
        st.caption(f"Role: {st.session_state['profile'].get('role')}")
        st.divider()
        if st.button("\U0001F504 Refresh", use_container_width=True):
            st.rerun()
        if st.button("Log out", use_container_width=True):
            auth.sign_out()
            st.rerun()


def render_dashboard_header(title: str, badge_text: str = "") -> None:
    """Branded top bar + explicit Refresh and Log out buttons beside it (in
    addition to the same two already in the sidebar), so neither requires
    hunting for it from within a dashboard."""
    col_title, col_refresh, col_logout = st.columns([4.5, 1, 1])
    with col_title:
        st.markdown(
            f"""
            <div class="app-header">
                <div class="title-group">
                    <div class="logo-mark">\U0001F4CA</div>
                    <h1>{title}</h1>
                </div>
                <div class="badge">{badge_text}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_refresh:
        st.markdown('<div style="height:0.55rem;"></div>', unsafe_allow_html=True)
        if st.button("\U0001F504 Refresh", key=f"refresh_{title}", use_container_width=True):
            st.rerun()
    with col_logout:
        st.markdown('<div style="height:0.55rem;"></div>', unsafe_allow_html=True)
        if st.button("Log out", key=f"logout_{title}", use_container_width=True):
            auth.sign_out()
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


def format_ranking_display(ranked_df: pd.DataFrame) -> pd.DataFrame:
    display_df = ranked_df[["bu_name", "bu_code", "rank", "status", "file_name", "submitted_at"]].copy()
    display_df["rank"] = display_df["rank"].apply(lambda r: int(r) if pd.notna(r) else None)
    display_df["submitted_at"] = display_df["submitted_at"].apply(ranking.format_mmt)
    return display_df.rename(columns=RANKING_DISPLAY_COLUMNS)


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

    today = ranking.today_mmt()
    month_year = today.strftime("%Y-%m")
    month_label = today.strftime("%B %Y")

    render_dashboard_header(f"\U0001F4E4 Submit {month_label} Report", bu_name)

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
        st.markdown(f"**Submitted at:** {ranking.format_mmt(existing['submitted_at'])} (MMT)")
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
                    flash(f"Report submitted! Status: {status}")
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


def render_overdue_alert(all_bus_df: pd.DataFrame, month_year: str) -> None:
    if ranking.now_mmt() <= ranking.get_deadline(month_year):
        return
    overdue = db.get_overdue_bus(month_year, all_bus_df)
    if overdue:
        st.error(f"⚠️ {len(overdue)} Business Unit(s) missed the deadline for {month_year}: {', '.join(overdue)}")


def render_overview_tab(all_bus_df: pd.DataFrame, month_year: str) -> None:
    reports_df = db.get_reports_for_month(month_year)
    render_admin_kpis(all_bus_df, reports_df, month_year)
    st.write("")

    render_overdue_alert(all_bus_df, month_year)

    st.markdown(section_header_html("\U0001F3C6", "Current Ranking", month_year), unsafe_allow_html=True)
    ranked_df = ranking.compute_rankings(all_bus_df, reports_df)
    if ranked_df.empty:
        st.markdown(empty_state_html("No Business Units registered yet."), unsafe_allow_html=True)
    else:
        st.dataframe(format_ranking_display(ranked_df), use_container_width=True, hide_index=True)


def render_ranking_tab(all_bus_df: pd.DataFrame, month_year: str) -> None:
    reports_df = db.get_reports_for_month(month_year)
    ranked_df = ranking.compute_rankings(all_bus_df, reports_df)

    bu_filter_options = ["All Business Units"] + (
        sorted(all_bus_df["bu_name"].tolist()) if not all_bus_df.empty else []
    )
    selected_bu = st.selectbox("Filter by Business Unit", bu_filter_options, key=f"bu_filter_{month_year}")
    filtered_df = ranked_df if selected_bu == "All Business Units" else ranked_df[ranked_df["bu_name"] == selected_bu]

    st.markdown(section_header_html("\U0001F4CB", "Ranking", f"{len(ranked_df)} Business Units"), unsafe_allow_html=True)
    if filtered_df.empty:
        st.markdown(empty_state_html("No data for this filter."), unsafe_allow_html=True)
    else:
        st.dataframe(format_ranking_display(filtered_df), use_container_width=True, hide_index=True)

    submitted_df = filtered_df[filtered_df["id"].notna()]
    if not submitted_df.empty:
        with st.expander("View / download submitted files"):
            for _, row in submitted_df.iterrows():
                st.markdown(f"- **{row['bu_name']}** — [{row['file_name']}]({row['file_url']})")

    # Reordering rank only makes sense against the full month's field of
    # submissions -- when narrowed to a single BU, lock rank editing so a
    # single-row "renumber" doesn't clobber that BU's true cross-BU rank.
    rank_editable = selected_bu == "All Business Units"
    st.markdown(section_header_html("\U0001F527", "Override Rank & Status"), unsafe_allow_html=True)
    if not rank_editable:
        st.caption("Switch to \"All Business Units\" to re-rank submissions against each other.")

    if submitted_df.empty:
        st.markdown(empty_state_html("No submissions to edit for this filter."), unsafe_allow_html=True)
    else:
        editable_df = submitted_df[["id", "bu_name", "rank", "status"]].copy()
        # A report can end up with no rank (e.g. legacy data from before
        # ranks were assigned at insert time) -- .astype(int) crashes on
        # NaN, so any missing rank is pushed to the bottom instead of
        # erroring the whole tab.
        fallback_rank = int(editable_df["rank"].max()) + 1 if editable_df["rank"].notna().any() else 1
        editable_df["rank"] = editable_df["rank"].fillna(fallback_rank).astype(int)
        edited_df = st.data_editor(
            editable_df,
            column_config={
                "id": None,  # hide the raw id column but keep it in the dataframe for updates
                "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
                "rank": st.column_config.NumberColumn("Rank", min_value=1, step=1, disabled=not rank_editable),
                "status": st.column_config.SelectboxColumn("Status", options=["Submitted", "Late", "Pending"]),
            },
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            key=f"admin_editor_{month_year}_{selected_bu}",
        )

        if st.button("\U0001F504 Save Ranking Changes", key=f"save_{month_year}_{selected_bu}", use_container_width=True):
            try:
                for i in range(len(editable_df)):
                    report_id = editable_df.iloc[i]["id"]
                    new_rank = int(edited_df.iloc[i]["rank"])
                    new_status = edited_df.iloc[i]["status"]
                    db.update_report_rank_status(report_id, new_rank, new_status)
                flash("Ranking changes saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not save changes: {exc}")

        with st.expander("⚠️ Danger Zone: Delete a submission"):
            options = {f"{row['bu_name']} — {row['file_name']}": row for _, row in submitted_df.iterrows()}
            choice = st.selectbox(
                "Select a submission to permanently delete", list(options.keys()), key=f"del_select_{month_year}"
            )
            confirm = st.checkbox(
                "I understand this permanently deletes the file and its ranking record. Remaining ranks will renumber.",
                key=f"del_confirm_{month_year}",
            )
            if st.button("Delete Submission", key=f"del_btn_{month_year}", disabled=not confirm, use_container_width=True):
                try:
                    row = options[choice]
                    db.delete_report(row["id"], row["file_path"], month_year)
                    flash(f"Deleted {choice}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not delete: {exc}")

    st.markdown(section_header_html("\U0001F4E4", "Export"), unsafe_allow_html=True)
    csv_bytes = format_ranking_display(ranked_df).to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download This Month's Ranking (CSV)",
        data=csv_bytes,
        file_name=f"ranking_{month_year}.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"export_{month_year}",
    )


def render_pending_approvals(all_bus_df: pd.DataFrame) -> None:
    pending_df = db.get_pending_profiles()
    st.markdown(section_header_html("✅", "Pending User Approvals", f"{len(pending_df)} awaiting review"), unsafe_allow_html=True)

    if pending_df.empty:
        st.markdown(empty_state_html("No pending registrations."), unsafe_allow_html=True)
        return

    bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}

    for _, row in pending_df.iterrows():
        col_info, col_approve, col_reject = st.columns([3, 1, 1])
        with col_info:
            bu_name = bu_lookup.get(row["bu_id"], "—")
            st.markdown(f"**{row['full_name']}** — {bu_name}  \n<span style='color:#475569;font-size:0.8rem;'>Requested {ranking.format_mmt(row['created_at'])} (MMT)</span>", unsafe_allow_html=True)
        with col_approve:
            if st.button("Approve", key=f"approve_{row['id']}", use_container_width=True):
                db.update_profile_status(row["id"], "approved")
                flash(f"Approved {row['full_name']}.")
                st.rerun()
        with col_reject:
            if st.button("Reject", key=f"reject_{row['id']}", use_container_width=True):
                db.update_profile_status(row["id"], "rejected")
                flash(f"Rejected {row['full_name']}.", kind="warning")
                st.rerun()


def render_role_management_tab(all_bus_df: pd.DataFrame) -> None:
    render_pending_approvals(all_bus_df)
    st.divider()

    st.markdown(section_header_html("\U0001F3E2", "Business Units", f"{len(all_bus_df)} total"), unsafe_allow_html=True)
    st.caption("Business Units are created by users at registration, not by admin.")
    if all_bus_df.empty:
        st.markdown(empty_state_html("No Business Units yet."), unsafe_allow_html=True)
    else:
        bu_search = st.text_input("Search Business Units", key="bu_search", placeholder="Type to filter...")
        bu_display_df = all_bus_df
        if bu_search:
            bu_display_df = all_bus_df[all_bus_df["bu_name"].str.contains(bu_search, case=False, na=False)]
        bu_display_df = bu_display_df.copy()
        bu_display_df["created_at"] = bu_display_df["created_at"].apply(ranking.format_mmt)
        st.dataframe(
            bu_display_df.rename(columns={"bu_name": "Business Unit", "bu_code": "Code", "created_at": "Created At (MMT)"}),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    profiles_df = db.get_all_profiles()
    st.markdown(section_header_html("\U0001F465", "User Control", f"{len(profiles_df)} accounts"), unsafe_allow_html=True)
    st.caption("Change a user's role or status, then Apply Changes. Admin does not create accounts here -- only approves (above) and manages existing ones.")

    if profiles_df.empty:
        st.markdown(empty_state_html("No user accounts yet."), unsafe_allow_html=True)
        return

    bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}
    profiles_df["bu_name"] = profiles_df["bu_id"].map(bu_lookup)

    user_search = st.text_input("Search users", key="user_search", placeholder="Search by name or Business Unit...")
    filtered_profiles_df = profiles_df
    if user_search:
        mask = profiles_df["full_name"].str.contains(user_search, case=False, na=False) | profiles_df["bu_name"].str.contains(
            user_search, case=False, na=False
        )
        filtered_profiles_df = profiles_df[mask]

    editable_df = filtered_profiles_df[["full_name", "bu_name", "role", "status", "created_at"]].copy()
    editable_df["created_at"] = editable_df["created_at"].apply(ranking.format_mmt)
    edited_df = st.data_editor(
        editable_df,
        column_config={
            "full_name": st.column_config.TextColumn("Name", disabled=True),
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "role": st.column_config.SelectboxColumn("Role", options=["bu_user", "admin"]),
            "status": st.column_config.SelectboxColumn("Status", options=["pending", "approved", "rejected"]),
            "created_at": st.column_config.TextColumn("Registered At (MMT)", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="user_control_editor",
    )

    if st.button("Apply Changes", key="apply_user_control", use_container_width=True):
        try:
            db.apply_profile_changes(filtered_profiles_df, edited_df)
            flash("User accounts updated.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not apply changes: {exc}")

    with st.expander("⚠️ Danger Zone: Delete a user"):
        deletable_df = filtered_profiles_df[filtered_profiles_df["role"] != "admin"]
        if deletable_df.empty:
            st.caption("No deletable users (admin accounts can't be deleted here).")
        else:
            delete_options = {f"{row['full_name']} — {row['bu_name']}": row["id"] for _, row in deletable_df.iterrows()}
            delete_choice = st.selectbox(
                "Select a user to permanently delete", list(delete_options.keys()), key="delete_user_select"
            )
            delete_confirm = st.checkbox(
                "I understand this permanently deletes this user's login and cannot be undone.",
                key="delete_user_confirm",
            )
            if st.button("Delete User", key="delete_user_btn", disabled=not delete_confirm, use_container_width=True):
                try:
                    db.delete_user_account(delete_options[delete_choice])
                    flash(f"Deleted {delete_choice}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not delete user: {exc}")


def render_analytics_tab(all_bus_df: pd.DataFrame) -> None:
    history_df = db.get_all_reports_history()

    st.markdown(section_header_html("\U0001F4C8", "Rank Trend by Business Unit"), unsafe_allow_html=True)

    if history_df.empty:
        st.markdown(empty_state_html("No submission history yet."), unsafe_allow_html=True)
    elif history_df["month_year"].nunique() < 2:
        st.markdown(
            empty_state_html("Trend needs at least two months of submissions -- check back after next month's reports."),
            unsafe_allow_html=True,
        )
    else:
        bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}
        plot_source = history_df.copy()
        plot_source["bu_name"] = plot_source["bu_id"].map(bu_lookup)
        plot_source = plot_source.dropna(subset=["bu_name", "rank"])

        if plot_source.empty:
            st.markdown(empty_state_html("No ranked submissions to plot yet."), unsafe_allow_html=True)
        else:
            by_activity = plot_source["bu_name"].value_counts().index.tolist()
            all_bu_names = sorted(by_activity)

            st.caption(
                f"{len(all_bu_names)} Business Units in history -- pick up to {MAX_TREND_SERIES} to plot at once "
                "(beyond that, lines stop being visually distinguishable)."
            )

            default_selection = sorted(by_activity[: min(5, len(by_activity))])
            selected = st.multiselect(
                "Business units to plot", options=all_bu_names, default=default_selection, key="trend_bu_multiselect"
            )

            if not selected:
                st.caption("Select at least one Business Unit to see its rank trend.")
            else:
                if len(selected) > MAX_TREND_SERIES:
                    st.warning(
                        f"Showing the first {MAX_TREND_SERIES} of your {len(selected)} selected Business Units -- "
                        "deselect some to see the rest."
                    )
                    selected = selected[:MAX_TREND_SERIES]

                plot_df = plot_source[plot_source["bu_name"].isin(selected)]
                months_sorted = sorted(plot_df["month_year"].unique())
                selected_sorted = sorted(selected)
                # Color scale is keyed to the current selection (sorted, so
                # order is stable while the selection itself doesn't
                # change) -- with more BUs than palette slots, no fixed
                # global assignment can hold for all of them.
                color_scale = alt.Scale(domain=selected_sorted, range=TREND_CHART_PALETTE[: len(selected_sorted)])

                line_layer = (
                    alt.Chart(plot_df)
                    .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True))
                    .encode(
                        x=alt.X("month_year:O", sort=months_sorted, title="Month", axis=alt.Axis(labelAngle=0)),
                        y=alt.Y(
                            "rank:Q",
                            title="Rank (1 = best)",
                            scale=alt.Scale(reverse=True),
                            axis=alt.Axis(tickMinStep=1),
                        ),
                        color=alt.Color("bu_name:N", scale=color_scale, title="Business Unit"),
                        tooltip=[
                            alt.Tooltip("bu_name:N", title="Business Unit"),
                            alt.Tooltip("month_year:O", title="Month"),
                            alt.Tooltip("rank:Q", title="Rank"),
                            alt.Tooltip("status:N", title="Status"),
                        ],
                    )
                )

                layers = [line_layer]
                if len(selected) <= 4:
                    # Direct-label small series counts in addition to the legend.
                    last_points = plot_df.sort_values("month_year").groupby("bu_name", as_index=False).last()
                    layers.append(
                        alt.Chart(last_points)
                        .mark_text(align="left", dx=8, fontSize=11, fontWeight="bold")
                        .encode(
                            x=alt.X("month_year:O", sort=months_sorted),
                            y=alt.Y("rank:Q", scale=alt.Scale(reverse=True)),
                            text="bu_name:N",
                            color=alt.Color("bu_name:N", scale=color_scale, legend=None),
                        )
                    )

                chart = alt.layer(*layers).properties(height=320).interactive()
                st.altair_chart(chart, use_container_width=True)

                with st.expander("View as table"):
                    pivot = plot_df.pivot_table(
                        index="month_year", columns="bu_name", values="rank", aggfunc="first"
                    ).sort_index()
                    st.dataframe(pivot, use_container_width=True)

    st.divider()
    st.markdown(section_header_html("\U0001F4E4", "Export Full History"), unsafe_allow_html=True)
    if history_df.empty:
        st.markdown(empty_state_html("Nothing to export yet."), unsafe_allow_html=True)
    else:
        bu_lookup = dict(zip(all_bus_df["id"], all_bus_df["bu_name"])) if not all_bus_df.empty else {}
        export_df = history_df.copy()
        export_df["bu_name"] = export_df["bu_id"].map(bu_lookup)
        export_df["submitted_at"] = export_df["submitted_at"].apply(lambda ts: ranking.format_mmt(ts, "%Y-%m-%d %H:%M:%S"))
        export_df = export_df.rename(columns={"submitted_at": "submitted_at_mmt"})
        csv_bytes = export_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Full Ranking History (CSV)",
            data=csv_bytes,
            file_name=f"ranking_history_{ranking.today_mmt().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )


def render_admin_dashboard() -> None:
    render_dashboard_header("\U0001F4CA Admin Dashboard", "Admin")

    all_bus_df = db.get_business_units()
    months = db.get_available_months()
    current_month = ranking.today_mmt().strftime("%Y-%m")
    month_options = sorted(set(months) | {current_month}, reverse=True)
    selected_month = st.selectbox("Reporting period", month_options, index=0)

    tab_overview, tab_rankings, tab_users, tab_analytics = st.tabs(
        ["\U0001F4CB Overview", "\U0001F3C6 Rankings", "\U0001F465 Users & BUs", "\U0001F4C8 Analytics"]
    )
    with tab_overview:
        render_overview_tab(all_bus_df, selected_month)
    with tab_rankings:
        render_ranking_tab(all_bus_df, selected_month)
    with tab_users:
        render_role_management_tab(all_bus_df)
    with tab_analytics:
        render_analytics_tab(all_bus_df)


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
    render_flash()

    if auth.is_admin():
        render_admin_dashboard()
    else:
        render_bu_dashboard()


if __name__ == "__main__":
    main()
