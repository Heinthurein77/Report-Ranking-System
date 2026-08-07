

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import altair as alt
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
DEADLINE_DAY_OF_MONTH = 14  # submissions due within the first 14 days of each month

MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))  # fixed offset, no DST

STATUS_OPTIONS = ["Submitted", "Under Review", "Incomplete / Needs Fix", "Approved"]
USER_STATUS_OPTIONS = ["pending", "approved", "rejected", "suspended"]
RANK_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}

# Colorblind-validated categorical palette, fixed order (never cycled/reassigned
# per filter change) -- see dataviz skill references/palette.md.
TREND_CHART_PALETTE = [
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
]
MAX_TREND_SERIES = len(TREND_CHART_PALETTE)


# ===========================================================================
# Custom CSS — restrained, enterprise-dashboard look
# ===========================================================================
def inject_custom_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --color-bg: #F7F8FA;
            --color-surface: #FFFFFF;
            --color-border: #E4E7EC;
            --color-text: #101828;
            --color-text-muted: #667085;
            --color-primary: #3730A3;
            --color-primary-hover: #2E2585;
            --color-sidebar: #0B1120;
            --shadow-sm: 0 1px 2px rgba(16, 24, 40, 0.06);
            --shadow-md: 0 2px 8px rgba(16, 24, 40, 0.08);
        }

        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            color: var(--color-text);
        }

        .stApp { background: var(--color-bg); }

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

        h4 {
            font-weight: 600 !important;
            letter-spacing: -0.01em;
            color: var(--color-text) !important;
        }

        /* Section headers: a consistent, bordered header bar for every
           dashboard section instead of a bare markdown heading */
        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 0.55rem;
            margin: 0.4rem 0 1rem 0;
            border-bottom: 1px solid var(--color-border);
        }
        .section-header .section-title {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            font-size: 0.95rem;
            font-weight: 600;
            color: var(--color-text);
        }
        .section-header .section-icon {
            width: 26px;
            height: 26px;
            border-radius: 7px;
            background: #EEF2FF;
            color: var(--color-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            flex-shrink: 0;
        }
        .section-header .section-meta {
            font-size: 0.78rem;
            color: var(--color-text-muted);
            font-weight: 500;
        }

        /* Compact stat tiles (BU dashboard summary row) */
        .stat-tile {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: 10px;
            padding: 0.9rem 1.1rem;
            box-shadow: var(--shadow-sm);
        }
        .stat-tile .stat-label {
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--color-text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .stat-tile .stat-value {
            font-size: 1.35rem;
            font-weight: 700;
            color: var(--color-text);
            margin-top: 0.25rem;
        }
        .stat-tile .stat-value.accent { color: var(--color-primary); }
        .stat-tile .stat-value.warn { color: #B54708; }
        .stat-tile .stat-value.danger { color: #B42318; }

        /* Empty state */
        .empty-state {
            background: var(--color-surface);
            border: 1px dashed var(--color-border);
            border-radius: 10px;
            padding: 1.5rem;
            text-align: center;
            color: var(--color-text-muted);
            font-size: 0.85rem;
        }

        /* App header: solid, low-contrast chrome rather than a bright banner */
        .app-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0.95rem 1.5rem;
            background: var(--color-sidebar);
            border-radius: 12px;
            color: #fff;
            margin-bottom: 1.75rem;
        }
        .app-header .title-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .app-header .logo-mark {
            width: 34px;
            height: 34px;
            border-radius: 8px;
            background: var(--color-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.05rem;
            flex-shrink: 0;
        }
        .app-header h1 {
            font-size: 1.05rem;
            font-weight: 600;
            letter-spacing: -0.01em;
            margin: 0;
            color: #fff;
        }
        .app-header span.subtitle {
            font-size: 0.8rem;
            color: #94A3B8;
            font-weight: 400;
        }
        .app-header .badge {
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            padding: 0.3rem 0.85rem;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 500;
            color: #E2E8F0;
        }

        /* Cards */
        .metric-card {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: 10px;
            padding: 1.15rem 1.3rem;
            box-shadow: var(--shadow-sm);
            height: 100%;
        }
        .metric-card .rank-badge {
            font-size: 1.5rem;
            line-height: 1;
        }
        .metric-card .bu-name {
            font-size: 1.05rem;
            font-weight: 600;
            color: var(--color-text);
            margin: 0.35rem 0 0.15rem 0;
        }
        .metric-card .score {
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--color-primary);
        }
        .metric-card .label {
            font-size: 0.72rem;
            font-weight: 600;
            color: var(--color-text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .metric-card.gold { border-top: 3px solid #B45309; }
        .metric-card.silver { border-top: 3px solid #64748B; }

        .info-card {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: 10px;
            padding: 0.9rem 1.1rem;
            box-shadow: var(--shadow-sm);
        }

        /* Status badges — color-coded by meaning, not one-size-fits-all green */
        .status-badge {
            padding: 3px 11px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 0.75rem;
            display: inline-block;
            border: 1px solid transparent;
        }
        .status-badge.badge-green  { background: #ECFDF3; color: #027A48; border-color: #ABEFC6; }
        .status-badge.badge-blue   { background: #EFF4FF; color: #175CD3; border-color: #B2CCFF; }
        .status-badge.badge-amber  { background: #FFFAEB; color: #B54708; border-color: #FEDF89; }
        .status-badge.badge-red    { background: #FEF3F2; color: #B42318; border-color: #FECDCA; }
        .status-badge.badge-gray   { background: #F2F4F7; color: #344054; border-color: #E4E7EC; }

        /* Buttons: solid, low-elevation, darken (not glow) on hover */
        .stButton > button, .stDownloadButton > button {
            border-radius: 8px;
            font-weight: 600;
            font-size: 0.9rem;
            border: 1px solid var(--color-primary);
            background: var(--color-primary);
            color: #fff;
            padding: 0.5rem 1.2rem;
            box-shadow: var(--shadow-sm);
            transition: background 0.12s ease;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            background: var(--color-primary-hover);
            border-color: var(--color-primary-hover);
            color: #fff;
        }

        /* File uploader */
        [data-testid="stFileUploader"] {
            border: 1.5px dashed var(--color-border);
            border-radius: 10px;
            padding: 0.75rem;
            background: var(--color-surface);
        }

        /* Tables */
        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border-radius: 10px;
            overflow: hidden;
            border: 1px solid var(--color-border);
            box-shadow: var(--shadow-sm);
        }

        /* Login screen */
        .login-wrapper {
            max-width: 396px;
            margin: 6vh auto 0 auto;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: 16px;
            padding: 2.3rem 2.3rem 1.7rem 2.3rem;
            box-shadow: 0 4px 24px rgba(16, 24, 40, 0.08);
        }
        .login-wrapper .logo-mark {
            width: 46px;
            height: 46px;
            border-radius: 11px;
            background: var(--color-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.4rem;
            margin: 0 auto 1.1rem auto;
            box-shadow: 0 4px 10px rgba(55, 48, 163, 0.28);
        }
        .login-wrapper h2 {
            text-align: center;
            font-weight: 700;
            font-size: 1.25rem;
            letter-spacing: -0.01em;
            color: var(--color-text);
            margin-bottom: 0.25rem;
        }
        .login-wrapper p.subtitle {
            text-align: center;
            color: var(--color-text-muted);
            font-size: 0.85rem;
            margin-bottom: 1.75rem;
        }
        .login-wrapper p.footnote {
            text-align: center;
            color: var(--color-text-muted);
            font-size: 0.78rem;
            margin-top: 0.9rem;
        }

        /* Pill-style tab control (used by the login/register switcher) */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 4px;
            background: var(--color-bg);
            padding: 4px;
            border-radius: 10px;
            border: 1px solid var(--color-border);
        }
        [data-testid="stTabs"] [data-baseweb="tab-highlight"],
        [data-testid="stTabs"] [data-baseweb="tab-border"] {
            display: none;
        }
        [data-testid="stTabs"] button[data-baseweb="tab"] {
            flex: 1;
            justify-content: center;
            border-radius: 7px;
            padding: 0.5rem 0;
            color: var(--color-text-muted);
            font-weight: 600;
            font-size: 0.85rem;
        }
        [data-testid="stTabs"] button[aria-selected="true"] {
            background: var(--color-surface);
            color: var(--color-primary);
            box-shadow: var(--shadow-sm);
        }
        [data-testid="stTabs"] [data-testid="stMarkdownContainer"] p {
            font-size: 0.85rem;
        }

        /* Text inputs */
        .stTextInput input {
            border-radius: 8px !important;
            border: 1px solid var(--color-border) !important;
            padding: 0.55rem 0.75rem !important;
            font-size: 0.9rem !important;
        }
        .stTextInput input:focus {
            border-color: var(--color-primary) !important;
            box-shadow: 0 0 0 3px rgba(55, 48, 163, 0.12) !important;
        }
        .stTextInput label p {
            font-size: 0.8rem !important;
            font-weight: 600 !important;
            color: var(--color-text) !important;
        }

        [data-testid="stSidebar"] {
            background: var(--color-sidebar);
        }
        [data-testid="stSidebar"] * {
            color: #CBD5E1 !important;
        }
        [data-testid="stSidebar"] .avatar-circle {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: var(--color-primary);
            color: #fff !important;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.9rem;
            margin-bottom: 0.6rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


STATUS_BADGE_CLASSES = {
    "Submitted": "badge-blue",
    "Under Review": "badge-amber",
    "Incomplete / Needs Fix": "badge-red",
    "Approved": "badge-green",
    "pending": "badge-amber",
    "approved": "badge-green",
    "rejected": "badge-red",
    "suspended": "badge-gray",
}


def status_badge_html(status: str) -> str:
    css_class = STATUS_BADGE_CLASSES.get(status, "badge-gray")
    return f'<span class="status-badge {css_class}">{status}</span>'


def now_mmt() -> datetime:
    return datetime.now(MYANMAR_TZ)


def today_mmt() -> date:
    return now_mmt().date()


def parse_to_mmt(timestamp: Optional[str]) -> Optional[datetime]:
    """Parse a Supabase timestamptz string (UTC) and convert to Myanmar time."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYANMAR_TZ)


def format_mmt(timestamp: Optional[str], fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = parse_to_mmt(timestamp)
    return dt.strftime(fmt) if dt else ""


def is_late_submission(created_at: str) -> bool:
    """A submission is late if it landed after day DEADLINE_DAY_OF_MONTH of
    the month IN MYANMAR TIME, based on its own created_at (not "today") so
    history stays accurate regardless of when it's viewed."""
    dt = parse_to_mmt(created_at)
    if dt is None:
        return False
    return dt.day > DEADLINE_DAY_OF_MONTH


def late_badge_html(created_at: str) -> str:
    if is_late_submission(created_at):
        return '<span class="status-badge badge-red">\U0001F6AB Late</span>'
    return ""


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


def get_all_users() -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = (
        supabase.table("users")
        .select("id, username, role, bu_name, status, created_at")
        .order("created_at", desc=True)
        .execute()
    )
    return pd.DataFrame(resp.data or [])


def apply_user_status_changes(original_df: pd.DataFrame, edited_df: pd.DataFrame) -> None:
    supabase = get_supabase_client()
    for i in range(len(original_df)):
        user_id = original_df.iloc[i]["id"]
        old_status = original_df.iloc[i]["status"]
        new_status = edited_df.iloc[i]["status"]
        if new_status != old_status:
            supabase.table("users").update({"status": new_status}).eq("id", user_id).execute()


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
def upload_to_supabase_storage(uploaded_file, bu_name: str, month: str) -> dict:
    supabase = get_supabase_client()

    clean_bu_name = bu_name.strip().replace(" ", "_")
    ext = os.path.splitext(uploaded_file.name)[1] or ".xlsx"
    # One submission per BU per month is already enforced, so this name is
    # unique on its own -- no timestamp needed, and it downloads with a
    # meaningful name instead of the BU's original (often generic) filename.
    display_name = f"{clean_bu_name}_{month}{ext}"
    file_path = f"{clean_bu_name.lower()}/{display_name}"
    file_bytes = uploaded_file.getvalue()

    supabase.storage.from_(STORAGE_BUCKET).upload(
        path=file_path,
        file=file_bytes,
        file_options={"content-type": uploaded_file.type or "application/octet-stream"},
    )

    public_url = supabase.storage.from_(STORAGE_BUCKET).get_public_url(file_path)
    return {"file_url": public_url, "file_path": file_path, "file_name": display_name}


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
    storage_result = upload_to_supabase_storage(uploaded_file, bu_name, month)

    supabase = get_supabase_client()
    order = get_next_submission_order(month)
    record = {
        "bu_name": bu_name,
        "submission_month": month,
        "submission_order": order,
        "file_name": storage_result["file_name"],
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


def get_overdue_bus(month: str) -> list:
    """Approved BU accounts that have not submitted for the given month."""
    supabase = get_supabase_client()
    resp = supabase.table("users").select("bu_name").eq("role", "bu_user").eq("status", "approved").execute()
    all_bu_names = sorted({row["bu_name"] for row in (resp.data or []) if row.get("bu_name")})
    submitted = {s["bu_name"] for s in get_submissions_for_month(month)}
    return [bu for bu in all_bu_names if bu not in submitted]


def get_all_submissions_history() -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = (
        supabase.table("bu_submissions")
        .select("bu_name, submission_month, submission_order, status, file_name, uploaded_by, created_at")
        .order("submission_month")
        .order("submission_order")
        .execute()
    )
    return pd.DataFrame(resp.data or [])


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
            <div class="title-group">
                <div class="logo-mark">\U0001F4CA</div>
                <div>
                    <h1>BU Report Submission &amp; Tracking</h1>
                    <span class="subtitle">{subtitle}</span>
                </div>
            </div>
            <div class="badge">{role_label}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_header(icon: str, title: str, meta: str = "") -> None:
    meta_html = f'<span class="section-meta">{meta}</span>' if meta else ""
    st.markdown(
        f"""
        <div class="section-header">
            <div class="section-title"><span class="section-icon">{icon}</span>{title}</div>
            {meta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stat_tile(label: str, value: str, accent: str = "") -> None:
    css_class = f"stat-value {accent}".strip()
    st.markdown(
        f"""
        <div class="stat-tile">
            <div class="stat-label">{label}</div>
            <div class="{css_class}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(message: str) -> None:
    st.markdown(f'<div class="empty-state">{message}</div>', unsafe_allow_html=True)


DISPLAY_COLUMN_LABELS = {
    "submission_month": "Month",
    "bu_name": "Business Unit",
    "file_name": "File",
    "submission_order": "Rank",
    "status": "Status",
    "uploaded_by": "Uploaded By",
    "created_at": "Submitted At (MMT)",
    "file_url": "File Link",
}


def format_display_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "created_at" in df.columns:
        df["created_at"] = df["created_at"].apply(format_mmt)
    return df.rename(columns=DISPLAY_COLUMN_LABELS)


def render_sidebar() -> None:
    with st.sidebar:
        username = st.session_state.get("username") or ""
        initials = username[:2].upper() if username else "?"
        st.markdown(f'<div class="avatar-circle">{initials}</div>', unsafe_allow_html=True)
        st.markdown("**Account**")
        st.caption(f"{st.session_state.get('username')} · {st.session_state.get('role')}")
        if st.session_state.get("bu_name"):
            st.caption(f"Business Unit: {st.session_state.get('bu_name')}")
        st.divider()
        if st.button("\U0001F504 Refresh", use_container_width=True):
            st.rerun()
        if st.button("Log out", use_container_width=True):
            logout()


# ===========================================================================
# UI: Login page
# ===========================================================================
def render_login() -> None:
    st.markdown('<div class="login-wrapper">', unsafe_allow_html=True)
    st.markdown('<div class="logo-mark">\U0001F4CA</div>', unsafe_allow_html=True)
    st.markdown("<h2>BU Report Tracker</h2>", unsafe_allow_html=True)
    st.markdown('<p class="subtitle">Sign in to submit or review monthly reports</p>', unsafe_allow_html=True)

    login_tab, register_tab = st.tabs(["Log in", "Register"])

    with login_tab:
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("\U0001F464 Username", placeholder="e.g. bu_sales")
            password = st.text_input("\U0001F512 Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Log in", use_container_width=True)
        st.markdown('<p class="footnote">Forgot your password? Contact your admin.</p>', unsafe_allow_html=True)

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
                elif user["status"] == "suspended":
                    st.error("Your account has been suspended. Please contact your admin.")
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
            reg_username = st.text_input("\U0001F464 Choose a username", key="reg_username")
            reg_bu_name = st.text_input("\U0001F3E2 Business Unit name", key="reg_bu_name", placeholder="e.g. Sales BU")
            reg_password = st.text_input("\U0001F512 Choose a password", type="password", key="reg_password")
            reg_password_confirm = st.text_input("\U0001F512 Confirm password", type="password", key="reg_password_confirm")
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

    today = today_mmt()
    month = today.strftime("%Y-%m")
    month_label = today.strftime("%B %Y")
    days_remaining = DEADLINE_DAY_OF_MONTH - today.day

    existing = get_submission_for_bu_month(bu_name, month)
    history_df = get_submissions_for_bu(bu_name)

    stat_col1, stat_col2, stat_col3 = st.columns(3)
    with stat_col1:
        rank_value = f"#{existing['submission_order']}" if existing else "—"
        render_stat_tile("Current Rank", rank_value, "accent" if existing else "")
    with stat_col2:
        if existing:
            deadline_value, deadline_accent = "Submitted", "accent"
        elif days_remaining > 0:
            deadline_value, deadline_accent = f"{days_remaining}d left", "warn"
        else:
            deadline_value, deadline_accent = "Passed", "danger"
        render_stat_tile("This Month's Deadline", deadline_value, deadline_accent)
    with stat_col3:
        render_stat_tile("Total Submissions", str(len(history_df)))

    render_section_header("\U0001F4C5", "Reporting Period", month_label)

    if not existing:
        if days_remaining > 0:
            st.warning(f"⏳ {days_remaining} day(s) left until the submission deadline (day {DEADLINE_DAY_OF_MONTH} of the month).")
        else:
            st.error(f"⚠️ Deadline passed — submissions were due by day {DEADLINE_DAY_OF_MONTH} of the month. Please submit as soon as possible.")

    if existing:
        if is_late_submission(existing["created_at"]):
            st.error(
                f"\U0001F6AB Late Submission: this report was submitted after the day-{DEADLINE_DAY_OF_MONTH} "
                "deadline. It's still ranked normally, but please submit on time next month."
            )
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
                    <div class="label" style="margin-top:0.6rem;">Submitted on: {format_mmt(existing['created_at'], '%Y-%m-%d %H:%M')} (MMT)</div>
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
                    {status_badge_html(existing['status'])} {late_badge_html(existing['created_at'])}
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

    render_section_header("\U0001F4DC", "Your Submission History", f"{len(history_df)} total")
    if history_df.empty:
        render_empty_state("No submissions yet — upload your first report above.")
    else:
        display_cols = ["submission_month", "file_name", "submission_order", "status", "created_at", "file_url"]
        display_cols = [c for c in display_cols if c in history_df.columns]
        st.dataframe(format_display_df(history_df[display_cols]), use_container_width=True, hide_index=True)


# ===========================================================================
# UI: Admin dashboard
# ===========================================================================
def render_pending_approvals() -> None:
    pending_df = get_pending_users()
    render_section_header("✅", "Pending User Approvals", f"{len(pending_df)} awaiting review")

    if pending_df.empty:
        render_empty_state("No pending registrations.")
        return

    for _, row in pending_df.iterrows():
        col_info, col_approve, col_reject = st.columns([3, 1, 1])
        with col_info:
            st.markdown(
                f"**{row['username']}** — {row['bu_name']}  \n"
                f"<span style='color:#64748B;font-size:0.8rem;'>Requested {format_mmt(row['created_at'])} (MMT)</span>",
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


def render_user_management() -> None:
    users_df = get_all_users()
    render_section_header("\U0001F465", "User Management", f"{len(users_df)} accounts")

    if users_df.empty:
        render_empty_state("No users yet.")
        return

    editable_df = users_df[["username", "role", "bu_name", "status", "created_at"]].copy()
    editable_df["created_at"] = editable_df["created_at"].apply(format_mmt)

    edited_df = st.data_editor(
        editable_df,
        column_config={
            "username": st.column_config.TextColumn("Username", disabled=True),
            "role": st.column_config.TextColumn("Role", disabled=True),
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "status": st.column_config.SelectboxColumn("Status", options=USER_STATUS_OPTIONS),
            "created_at": st.column_config.TextColumn("Registered At (MMT)", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key="user_management_editor",
    )

    if st.button("Apply User Changes", key="apply_user_changes", use_container_width=True):
        try:
            apply_user_status_changes(users_df, edited_df)
            st.success("User accounts updated.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not apply changes: {exc}")


def render_admin_month_table(month: str, bu_filter: str) -> None:
    submissions = get_submissions_for_month(month)
    if bu_filter != "All Business Units":
        submissions = [s for s in submissions if s["bu_name"] == bu_filter]

    if not submissions:
        render_empty_state("No submissions match this filter.")
        return

    original_df = pd.DataFrame(submissions)
    original_df["timing"] = original_df["created_at"].apply(lambda ts: "Late" if is_late_submission(ts) else "On Time")
    editable_df = original_df[["submission_order", "bu_name", "file_name", "status", "timing", "created_at"]].copy()
    editable_df["created_at"] = editable_df["created_at"].apply(format_mmt)

    # Reordering rank only makes sense against the full month's field of
    # submissions -- when narrowed to a single BU, lock rank editing so a
    # single-row "renumber" doesn't clobber that BU's true cross-BU rank.
    rank_editable = bu_filter == "All Business Units"
    if not rank_editable:
        st.caption("Switch the Business Unit filter to \"All Business Units\" to re-rank submissions against each other.")

    edited_df = st.data_editor(
        editable_df,
        column_config={
            "submission_order": st.column_config.NumberColumn("Rank", min_value=1, step=1, disabled=not rank_editable),
            "bu_name": st.column_config.TextColumn("Business Unit", disabled=True),
            "file_name": st.column_config.TextColumn("File", disabled=True),
            "status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
            "timing": st.column_config.TextColumn("Timing", disabled=True),
            "created_at": st.column_config.TextColumn("Submitted At (MMT)", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        key=f"editor_{month}_{bu_filter}",
    )

    with st.expander("View / download original files"):
        for s in submissions:
            st.markdown(f"- **{s['bu_name']}** — [{s['file_name']}]({s['file_url']})")

    if st.button("Apply Changes", key=f"apply_{month}_{bu_filter}", use_container_width=True):
        try:
            apply_admin_overrides(original_df, edited_df)
            st.success("Changes applied.")
            st.rerun()
        except Exception as exc:
            st.error(f"Could not apply changes: {exc}")


def render_rank_trend_chart(history_df: pd.DataFrame) -> None:
    render_section_header("\U0001F4C8", "Rank Trend by Business Unit")

    if history_df.empty:
        render_empty_state("No submission history yet.")
        return

    if history_df["submission_month"].nunique() < 2:
        render_empty_state("Trend needs at least two months of submissions -- check back after next month's reports.")
        return

    by_activity = history_df["bu_name"].value_counts().index.tolist()
    all_bu_names = sorted(by_activity)

    st.caption(
        f"{len(all_bu_names)} Business Units in history -- pick up to {MAX_TREND_SERIES} to plot at once "
        "(beyond that, lines stop being visually distinguishable). Use the CSV export below for the complete history."
    )

    default_selection = sorted(by_activity[: min(5, len(by_activity))])
    selected = st.multiselect(
        "Business units to plot",
        options=all_bu_names,
        default=default_selection,
        key="trend_bu_multiselect",
    )

    if not selected:
        st.caption("Select at least one Business Unit to see its rank trend.")
        return

    if len(selected) > MAX_TREND_SERIES:
        st.warning(f"Showing the first {MAX_TREND_SERIES} of your {len(selected)} selected Business Units -- deselect some to see the rest.")
        selected = selected[:MAX_TREND_SERIES]

    plot_df = history_df[history_df["bu_name"].isin(selected)].copy()
    months_sorted = sorted(plot_df["submission_month"].unique())

    # Color scale is keyed to the current selection (sorted, so order is
    # stable while the selection itself doesn't change) -- with more BUs
    # than palette slots, no fixed global assignment can hold for all of
    # them, so colors are only guaranteed stable within one selection.
    selected_sorted = sorted(selected)
    color_scale = alt.Scale(domain=selected_sorted, range=TREND_CHART_PALETTE[: len(selected_sorted)])

    line_layer = (
        alt.Chart(plot_df)
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True))
        .encode(
            x=alt.X("submission_month:O", sort=months_sorted, title="Month", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("submission_order:Q", title="Rank (1 = best)", scale=alt.Scale(reverse=True), axis=alt.Axis(tickMinStep=1)),
            color=alt.Color("bu_name:N", scale=color_scale, title="Business Unit"),
            tooltip=[
                alt.Tooltip("bu_name:N", title="Business Unit"),
                alt.Tooltip("submission_month:O", title="Month"),
                alt.Tooltip("submission_order:Q", title="Rank"),
                alt.Tooltip("status:N", title="Status"),
            ],
        )
    )

    layers = [line_layer]
    if len(selected) <= 4:
        # Direct-label small series counts in addition to the legend.
        last_points = plot_df.sort_values("submission_month").groupby("bu_name", as_index=False).last()
        layers.append(
            alt.Chart(last_points)
            .mark_text(align="left", dx=8, fontSize=11, fontWeight="bold")
            .encode(
                x=alt.X("submission_month:O", sort=months_sorted),
                y=alt.Y("submission_order:Q", scale=alt.Scale(reverse=True)),
                text="bu_name:N",
                color=alt.Color("bu_name:N", scale=color_scale, legend=None),
            )
        )

    chart = alt.layer(*layers).properties(height=320).interactive()
    st.altair_chart(chart, use_container_width=True)

    with st.expander("View as table"):
        pivot = plot_df.pivot_table(
            index="submission_month", columns="bu_name", values="submission_order", aggfunc="first"
        ).sort_index()
        st.dataframe(pivot, use_container_width=True)


def render_ranking_export(history_df: pd.DataFrame) -> None:
    render_section_header("\U0001F4E4", "Export Ranking History", f"{len(history_df)} records")

    if history_df.empty:
        render_empty_state("Nothing to export yet.")
        return

    export_df = history_df.copy()
    export_df["created_at"] = export_df["created_at"].apply(lambda ts: format_mmt(ts, "%Y-%m-%d %H:%M:%S"))
    export_df = export_df.rename(columns={"created_at": "created_at_mmt"})

    csv_bytes = export_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Ranking History (CSV)",
        data=csv_bytes,
        file_name=f"ranking_history_{today_mmt().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        use_container_width=True,
    )


def render_admin_overview_tab() -> None:
    months = get_available_months()
    if not months:
        render_empty_state("No submissions have been made yet.")
        return

    col_month, col_bu = st.columns(2)
    with col_month:
        selected_month = st.selectbox("Reporting month", months, index=0)
    with col_bu:
        bu_options = ["All Business Units"] + sorted(
            {s["bu_name"] for s in get_submissions_for_month(selected_month)}
        )
        selected_bu = st.selectbox("Business unit", bu_options, index=0)

    render_section_header("\U0001F3C6", "Top Submissions")
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
                        {status_badge_html(match['status'])} {late_badge_html(match['created_at'])}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    render_section_header("\U0001F4CB", "Submissions & Ranking Overrides")
    render_admin_month_table(selected_month, selected_bu)


def render_admin_users_tab() -> None:
    render_pending_approvals()
    st.divider()
    render_user_management()


def render_admin_analytics_tab() -> None:
    history_df = get_all_submissions_history()
    render_rank_trend_chart(history_df)
    st.divider()
    render_ranking_export(history_df)


def render_admin_dashboard() -> None:
    render_app_header("Review submissions and manage rankings")

    today = today_mmt()
    if today.day > DEADLINE_DAY_OF_MONTH:
        overdue = get_overdue_bus(today.strftime("%Y-%m"))
        if overdue:
            st.error(
                f"⚠️ {len(overdue)} Business Unit(s) missed the day-{DEADLINE_DAY_OF_MONTH} deadline "
                f"for {today.strftime('%B %Y')}: {', '.join(overdue)}"
            )

    overview_tab, users_tab, analytics_tab = st.tabs(["\U0001F4CA Overview", "\U0001F465 Users", "\U0001F4C8 Analytics"])
    with overview_tab:
        render_admin_overview_tab()
    with users_tab:
        render_admin_users_tab()
    with analytics_tab:
        render_admin_analytics_tab()


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
