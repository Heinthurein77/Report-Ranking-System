"""
BU Flexible Report Submission & Tracking System
------------------------------------------------
Single-file Streamlit app deployed on Streamlit Community Cloud.

Stack:
- Auth + data: Supabase (users, bu_submissions tables)
- File storage: Supabase Storage bucket "bu-reports" (public bucket).
  No Google Drive / OAuth involved -- files are stored and served straight
  from Supabase, which is far simpler to set up than Drive's service-account
  or OAuth flows.
- Ranking: submission ARRIVAL ORDER for the month (no Excel content is parsed
  or validated -- files are accepted and stored as-is). Each BU may submit
  once per month; admin can manually override rank/status afterwards.

Required Supabase schema (create these tables before running the app):

    create table users (
        id uuid primary key default gen_random_uuid(),
        username text unique not null,
        password_hash text not null,       -- bcrypt hash, see hash_password() below
        role text not null check (role in ('admin', 'bu_user')) default 'bu_user',
        bu_name text,
        status text not null check (status in ('pending', 'approved', 'rejected')) default 'pending',
        created_at timestamptz default now()
    );

    create table bu_submissions (
        id uuid primary key default gen_random_uuid(),
        bu_name text not null,
        submission_month text not null,        -- 'YYYY-MM'
        submission_order int not null,         -- rank within the month; admin-editable
        file_name text not null,
        file_url text not null,
        file_path text not null,               -- storage object path, for cleanup/reference
        status text not null check (status in ('Submitted', 'Under Review', 'Incomplete / Needs Fix', 'Approved')) default 'Submitted',
        uploaded_by text not null,
        created_at timestamptz default now()
    );

Also create a Storage bucket named "bu-reports" (Supabase Dashboard ->
Storage -> New bucket -> name "bu-reports" -> Public bucket: ON), so
get_public_url() returns links that are directly viewable/downloadable.

-- The admin account is seeded manually (self-registration always creates a
-- 'bu_user' pending account) -- insert one row directly with role='admin'
-- and status='approved':
--
--   insert into users (username, password_hash, role, status)
--   values ('admin', '<bcrypt hash>', 'admin', 'approved');
--
-- New users self-register as role='bu_user', status='pending', and cannot
-- log in until an admin approves them from the Admin Dashboard.
"""

from datetime import date, datetime
from typing import Optional

import pandas as pd
import streamlit as st
from supabase import create_client, Client
import bcrypt

# ===========================================================================
# Page config & constants
# ===========================================================================
st.set_page_config(
    page_title="BU Report Submission & Tracking",
    page_icon="\U0001F4CA",
    layout="wide",
    initial_sidebar_state="expanded",
)

MAX_UPLOAD_MB = 15
STORAGE_BUCKET = "bu-reports"

STATUS_OPTIONS = ["Submitted", "Under Review", "Incomplete / Needs Fix", "Approved"]
RANK_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


# ===========================================================================
# Custom CSS — modern SaaS dashboard look
# ===========================================================================
def inject_custom_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        /* Hide default Streamlit chrome */
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        [data-testid="stToolbar"] {visibility: hidden; height: 0; position: fixed;}
        [data-testid="stDecoration"] {display: none;}
        [data-testid="stStatusWidget"] {visibility: hidden;}
        header[data-testid="stHeader"] {background: transparent;}

        .block-container {
            padding-top: 1.5rem;
            padding-bottom: 3rem;
            max-width: 1200px;
        }

        /* App header */
        .app-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 1.1rem 1.6rem;
            background: linear-gradient(135deg, #4F46E5 0%, #6366F1 100%);
            border-radius: 16px;
            color: #fff;
            margin-bottom: 1.6rem;
            box-shadow: 0 8px 24px rgba(79, 70, 229, 0.25);
        }
        .app-header h1 {
            font-size: 1.35rem;
            font-weight: 700;
            margin: 0;
            color: #fff;
        }
        .app-header span.subtitle {
            font-size: 0.85rem;
            opacity: 0.85;
            font-weight: 400;
        }
        .app-header .badge {
            background: rgba(255,255,255,0.18);
            padding: 0.35rem 0.9rem;
            border-radius: 999px;
            font-size: 0.8rem;
            font-weight: 600;
        }

        /* Cards */
        .metric-card {
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 16px;
            padding: 1.3rem 1.4rem;
            box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
            height: 100%;
        }
        .metric-card .rank-badge {
            font-size: 1.8rem;
            line-height: 1;
        }
        .metric-card .bu-name {
            font-size: 1.15rem;
            font-weight: 700;
            color: #0F172A;
            margin: 0.4rem 0 0.15rem 0;
        }
        .metric-card .score {
            font-size: 1.6rem;
            font-weight: 800;
            color: #4F46E5;
        }
        .metric-card .label {
            font-size: 0.78rem;
            font-weight: 600;
            color: #64748B;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .metric-card.gold { border-top: 4px solid #F59E0B; }
        .metric-card.silver { border-top: 4px solid #94A3B8; }

        .info-card {
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 14px;
            padding: 1rem 1.2rem;
            box-shadow: 0 2px 10px rgba(15, 23, 42, 0.04);
        }

        .status-badge {
            background-color: #dcfce7;
            color: #15803d;
            padding: 4px 12px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 0.8rem;
            display: inline-block;
        }

        /* Buttons */
        .stButton > button, .stDownloadButton > button {
            border-radius: 10px;
            font-weight: 600;
            border: none;
            background: linear-gradient(135deg, #4F46E5 0%, #6366F1 100%);
            color: #fff;
            padding: 0.55rem 1.3rem;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.25);
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            filter: brightness(1.08);
            color: #fff;
        }

        /* File uploader */
        [data-testid="stFileUploader"] {
            border: 2px dashed #C7D2FE;
            border-radius: 14px;
            padding: 0.8rem;
            background: #F5F6FF;
        }

        /* Tables */
        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid #E5E7EB;
        }

        /* Login screen */
        .login-wrapper {
            max-width: 420px;
            margin: 4rem auto 0 auto;
            background: #FFFFFF;
            border: 1px solid #E5E7EB;
            border-radius: 18px;
            padding: 2.2rem 2.2rem 1.6rem 2.2rem;
            box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
        }
        .login-wrapper h2 {
            text-align: center;
            font-weight: 800;
            color: #0F172A;
            margin-bottom: 0.2rem;
        }
        .login-wrapper p.subtitle {
            text-align: center;
            color: #64748B;
            font-size: 0.9rem;
            margin-bottom: 1.6rem;
        }

        [data-testid="stSidebar"] {
            background: #0F172A;
        }
        [data-testid="stSidebar"] * {
            color: #E2E8F0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ===========================================================================
# Cached resource connections
# ===========================================================================
@st.cache_resource(show_spinner=False)
def get_supabase_client() -> Client:
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    except KeyError:
        st.error("Supabase credentials are missing from st.secrets. Please configure `SUPABASE_URL` and `SUPABASE_KEY` in secrets.toml.")
        st.stop()
    return create_client(url, key)


# ===========================================================================
# Auth helpers
# ===========================================================================
def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def authenticate(username: str, password: str) -> Optional[dict]:
    supabase = get_supabase_client()
    try:
        resp = supabase.table("users").select("*").eq("username", username).limit(1).execute()
    except Exception as exc:
        st.error(f"Could not reach the authentication service: {exc}")
        return None

    rows = resp.data or []
    if not rows:
        return None

    user = rows[0]
    if verify_password(password, user["password_hash"]):
        return user
    return None


def username_exists(username: str) -> bool:
    supabase = get_supabase_client()
    resp = supabase.table("users").select("id").eq("username", username).limit(1).execute()
    return bool(resp.data)


def register_user(username: str, password: str, bu_name: str) -> None:
    """Self-registration always creates a pending BU user awaiting admin approval."""
    supabase = get_supabase_client()
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    record = {
        "username": username,
        "password_hash": password_hash,
        "role": "bu_user",
        "bu_name": bu_name,
        "status": "pending",
    }
    supabase.table("users").insert(record).execute()


def get_pending_users() -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = (
        supabase.table("users")
        .select("id, username, bu_name, created_at")
        .eq("status", "pending")
        .order("created_at")
        .execute()
    )
    return pd.DataFrame(resp.data or [])


def update_user_status(user_id: str, status: str) -> None:
    supabase = get_supabase_client()
    supabase.table("users").update({"status": status}).eq("id", user_id).execute()


def init_session_state() -> None:
    defaults = {
        "authenticated": False,
        "role": None,
        "username": None,
        "bu_name": None,
        "user_id": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def logout() -> None:
    for key in ["authenticated", "role", "username", "bu_name", "user_id"]:
        st.session_state.pop(key, None)
    st.rerun()


# ===========================================================================
# Supabase Storage helpers
# ===========================================================================
def upload_to_supabase_storage(uploaded_file, bu_name: str) -> dict:
    supabase = get_supabase_client()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_bu_name = bu_name.strip().lower().replace(" ", "_")
    file_path = f"{clean_bu_name}/{timestamp}_{uploaded_file.name}"
    file_bytes = uploaded_file.getvalue()

    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=file_path,
        file=file_bytes,
        file_options={"content-type": uploaded_file.type or "application/octet-stream"},
    )

    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_path)
    return {"file_url": public_url, "file_path": file_path}


# ===========================================================================
# Supabase data helpers — submissions
# ===========================================================================
def get_next_submission_order(month: str) -> int:
    supabase = get_supabase_client()
    resp = supabase.table("bu_submissions").select("id", count="exact").eq("submission_month", month).execute()
    return (resp.count or 0) + 1


def get_submission_for_bu_month(bu_name: str, month: str) -> Optional[dict]:
    supabase = get_supabase_client()
    resp = (
        supabase.table("bu_submissions")
        .select("*")
        .eq("bu_name", bu_name)
        .eq("submission_month", month)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def create_submission(bu_name: str, month: str, uploaded_file, uploaded_by: str) -> dict:
    storage_result = upload_to_supabase_storage(uploaded_file, bu_name)

    supabase = get_supabase_client()
    order = get_next_submission_order(month)
    record = {
        "bu_name": bu_name,
        "submission_month": month,
        "submission_order": order,
        "file_name": uploaded_file.name,
        "file_url": storage_result["file_url"],
        "file_path": storage_result["file_path"],
        "status": "Submitted",
        "uploaded_by": uploaded_by,
    }
    resp = supabase.table("bu_submissions").insert(record).execute()
    return resp.data[0]


def get_submissions_for_bu(bu_name: str) -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = (
        supabase.table("bu_submissions")
        .select("*")
        .eq("bu_name", bu_name)
        .order("created_at", desc=True)
        .execute()
    )
    return pd.DataFrame(resp.data or [])


def get_submissions_for_month(month: str) -> list:
    supabase = get_supabase_client()
    resp = (
        supabase.table("bu_submissions")
        .select("*")
        .eq("submission_month", month)
        .order("submission_order")
        .execute()
    )
    return resp.data or []


def get_available_months() -> list:
    supabase = get_supabase_client()
    resp = supabase.table("bu_submissions").select("submission_month").execute()
    months = sorted({row["submission_month"] for row in (resp.data or [])}, reverse=True)
    return months


def apply_admin_overrides(original_df: pd.DataFrame, edited_df: pd.DataFrame) -> None:
    """Persist admin edits made in the data editor: status changes are applied
    directly; submission_order changes are resolved into a strict 1..N
    ordering (ties broken by original row order), which naturally shifts
    every other BU's rank when one submission is moved up or down."""
    supabase = get_supabase_client()

    for i in range(len(original_df)):
        sub_id = original_df.iloc[i]["id"]
        old_status = original_df.iloc[i]["status"]
        new_status = edited_df.iloc[i]["status"]
        if new_status != old_status:
            supabase.table("bu_submissions").update({"status": new_status}).eq("id", sub_id).execute()

    requested = [(i, original_df.iloc[i]["id"], edited_df.iloc[i]["submission_order"]) for i in range(len(original_df))]
    requested_sorted = sorted(requested, key=lambda t: (t[2], t[0]))

    for new_order, (orig_idx, sub_id, _) in enumerate(requested_sorted, start=1):
        if new_order != original_df.iloc[orig_idx]["submission_order"]:
            supabase.table("bu_submissions").update({"submission_order": new_order}).eq("id", sub_id).execute()


# ===========================================================================
# UI: shared header
# ===========================================================================
def render_app_header(subtitle: str) -> None:
    role_label = "Admin" if st.session_state.get("role") == "admin" else st.session_state.get("bu_name", "")
    st.markdown(
        f"""
        <div class="app-header">
            <div>
                <h1>\U0001F4CA BU Report Submission &amp; Tracking</h1>
                <span class="subtitle">{subtitle}</span>
            </div>
            <div class="badge">{role_label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### \U0001F464 Account")
        st.write(f"**User:** {st.session_state.get('username')}")
        st.write(f"**Role:** {st.session_state.get('role')}")
        if st.session_state.get("bu_name"):
            st.write(f"**BU:** {st.session_state.get('bu_name')}")
        st.divider()
        if st.button("Log out", use_container_width=True):
            logout()


# ===========================================================================
# UI: Login page
# ===========================================================================
def render_login() -> None:
    st.markdown('<div class="login-wrapper">', unsafe_allow_html=True)
    st.markdown("<h2>\U0001F4CA BU Report Tracker</h2>", unsafe_allow_html=True)
    st.markdown('<p class="subtitle">Sign in to submit or review monthly reports</p>', unsafe_allow_html=True)

    login_tab, register_tab = st.tabs(["Log in", "Register"])

    with login_tab:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username", placeholder="e.g. bu_sales")
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Log in", use_container_width=True)

        if submitted:
            if not username or not password:
                st.error("Please enter both username and password.")
            else:
                with st.spinner("Verifying credentials..."):
                    user = authenticate(username.strip(), password)
                if user is None:
                    st.error("Invalid username or password.")
                elif user["status"] == "pending":
                    st.warning("Your registration is awaiting admin approval. Please check back later.")
                elif user["status"] == "rejected":
                    st.error("Your registration was rejected. Please contact your admin.")
                else:
                    st.session_state["authenticated"] = True
                    st.session_state["role"] = user["role"]
                    st.session_state["username"] = user["username"]
                    st.session_state["bu_name"] = user.get("bu_name")
                    st.session_state["user_id"] = user["id"]
                    st.rerun()

    with register_tab:
        st.caption("New Business Unit accounts require admin approval before you can log in.")
        with st.form("register_form", clear_on_submit=True):
            reg_username = st.text_input("Choose a username", key="reg_username")
            reg_bu_name = st.text_input("Business Unit name", key="reg_bu_name", placeholder="e.g. Sales BU")
            reg_password = st.text_input("Choose a password", type="password", key="reg_password")
            reg_password_confirm = st.text_input("Confirm password", type="password", key="reg_password_confirm")
            reg_submitted = st.form_submit_button("Request Access", use_container_width=True)

        if reg_submitted:
            if not reg_username or not reg_bu_name or not reg_password:
                st.error("Please fill in all fields.")
            elif len(reg_password) < 8:
                st.error("Password must be at least 8 characters.")
            elif reg_password != reg_password_confirm:
                st.error("Passwords do not match.")
            else:
                try:
                    if username_exists(reg_username.strip()):
                        st.error("That username is already taken.")
                    else:
                        register_user(reg_username.strip(), reg_password, reg_bu_name.strip())
                        st.success("Registration submitted! An admin must approve your account before you can log in.")
                except Exception as exc:
                    st.error(f"Registration failed: {exc}")

    st.markdown("</div>", unsafe_allow_html=True)


# ===========================================================================
# UI: BU user dashboard
# ===========================================================================
def render_bu_dashboard() -> None:
    render_app_header("Upload your month-end report")
    bu_name = st.session_state["bu_name"]

    if not bu_name:
        st.error("Your account is not linked to a Business Unit. Please contact your admin.")
        return

    today = date.today()
    month = today.strftime("%Y-%m")
    month_label = today.strftime("%B %Y")

    st.markdown(f"#### \U0001F4C5 Period: {month_label}")

    existing = get_submission_for_bu_month(bu_name, month)

    if existing:
        st.success(f"Your report for {month_label} has been submitted!")
        col1, col2 = st.columns(2)
        with col1:
            medal = RANK_MEDALS.get(existing["submission_order"], "\U0001F3C5")
            st.markdown(
                f"""
                <div class="metric-card">
                    <div class="label">Your Current Submission Order</div>
                    <div class="rank-badge">{medal}</div>
                    <div class="score">Rank #{existing['submission_order']}</div>
                    <div class="label" style="margin-top:0.6rem;">Submitted on: {existing['created_at'][:10]}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                f"""
                <div class="metric-card">
                    <div class="label">Uploaded File Details</div>
                    <div class="bu-name">{existing['file_name']}</div>
                    <span class="status-badge">{existing['status']}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown(f"[\U0001F4E5 View / Download File]({existing['file_url']})")
    else:
        st.info(
            "\U0001F4A1 You can upload ANY Excel file format (.xlsx, .xls). "
            "Rank is assigned automatically based on submission time, and you can submit once per month."
        )

        uploaded_file = st.file_uploader("Upload Excel Report", type=["xlsx", "xls"])

        if uploaded_file is not None:
            size_mb = uploaded_file.size / (1024 * 1024)
            if size_mb > MAX_UPLOAD_MB:
                st.error(f"File is {size_mb:.1f} MB, which exceeds the {MAX_UPLOAD_MB} MB limit.")
            else:
                st.caption(f"Ready to submit: **{uploaded_file.name}** ({size_mb:.2f} MB)")
                if st.button("Submit Report Now", use_container_width=True):
                    try:
                        with st.spinner("Uploading to Supabase Storage..."):
                            submission = create_submission(bu_name, month, uploaded_file, st.session_state["username"])
                        st.balloons()
                        st.success(f"Report uploaded successfully! You are Submission #{submission['submission_order']} for this month.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Submission failed: {exc}")

    st.markdown("#### \U0001F4DC Your Submission History")
    history_df = get_submissions_for_bu(bu_name)
    if history_df.empty:
        st.caption("No submissions yet.")
    else:
        display_cols = ["submission_month", "file_name", "submission_order", "status", "created_at", "file_url"]
        display_cols = [c for c in display_cols if c in history_df.columns]
        st.dataframe(history_df[display_cols], use_container_width=True, hide_index=True)


# ===========================================================================
# UI: Admin dashboard
# ===========================================================================
def render_pending_approvals() -> None:
    st.markdown("#### ✅ Pending User Approvals")
    pending_df = get_pending_users()

    if pending_df.empty:
        st.caption("No pending registrations.")
        return

    for _, row in pending_df.iterrows():
        col_info, col_approve, col_reject = st.columns([3, 1, 1])
        with col_info:
            st.markdown(
                f"**{row['username']}** — {row['bu_name']}  \n"
                f"<span style='color:#64748B;font-size:0.8rem;'>Requested {row['created_at']}</span>",
                unsafe_allow_html=True,
            )
        with col_approve:
            if st.button("Approve", key=f"approve_{row['id']}", use_container_width=True):
                update_user_status(row["id"], "approved")
                st.rerun()
        with col_reject:
            if st.button("Reject", key=f"reject_{row['id']}", use_container_width=True):
                update_user_status(row["id"], "rejected")
                st.rerun()


def render_admin_month_table(month: str) -> None:
    submissions = get_submissions_for_month(month)
    if not submissions:
        st.caption("No submissions for this month.")
        return

    original_df = pd.DataFrame(submissions)
    editable_df = original_df[["submission_order", "bu_name", "file_name", "status", "created_at"]].copy()

    edited_df = st.data_editor(
        editable_df,
        column_config={
            "submission_order": st.column_config.NumberColumn("Rank", min_value=1, step=1),
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "file_name": st.column_config.TextColumn("File", disabled=True),
            "status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
            "created_at": st.column_config.TextColumn("Submitted At", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key=f"editor_{month}",
    )

    with st.expander("View / download original files"):
        for s in submissions:
            st.markdown(f"- **{s['bu_name']}** — [{s['file_name']}]({s['file_url']})")

    if st.button("Apply Changes", key=f"apply_{month}", use_container_width=True):
        try:
            apply_admin_overrides(original_df, edited_df)
            st.success("Changes applied.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not apply changes: {exc}")


def render_admin_dashboard() -> None:
    render_app_header("Review submissions and manage rankings")

    render_pending_approvals()
    st.divider()

    months = get_available_months()
    if not months:
        st.info("No submissions have been made yet.")
        return

    selected_month = st.selectbox("Reporting month", months, index=0)

    st.markdown("#### \U0001F3C6 Top Submissions")
    top_submissions = get_submissions_for_month(selected_month)
    col1, col2 = st.columns(2)
    for col, rank in ((col1, 1), (col2, 2)):
        match = next((s for s in top_submissions if s["submission_order"] == rank), None)
        with col:
            if match is None:
                st.markdown(
                    f"""<div class="metric-card"><div class="label">Rank {rank}</div><div class="bu-name">No data</div></div>""",
                    unsafe_allow_html=True,
                )
            else:
                medal = RANK_MEDALS.get(rank, "\U0001F3C5")
                css_class = "gold" if rank == 1 else "silver"
                st.markdown(
                    f"""
                    <div class="metric-card {css_class}">
                        <div class="label">Rank {rank}</div>
                        <div class="rank-badge">{medal}</div>
                        <div class="bu-name">{match['bu_name']}</div>
                        <span class="status-badge">{match['status']}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    st.markdown("#### \U0001F4CB Submissions & Ranking Overrides")
    render_admin_month_table(selected_month)


# ===========================================================================
# Main
# ===========================================================================
def main() -> None:
    inject_custom_css()
    init_session_state()

    if not st.session_state["authenticated"]:
        render_login()
        return

    render_sidebar()

    if st.session_state["role"] == "admin":
        render_admin_dashboard()
    else:
        render_bu_dashboard()


if __name__ == "__main__":
    main()
