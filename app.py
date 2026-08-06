"""
BU Monthly Performance & Ranking System
----------------------------------------
Single-file Streamlit app deployed on Streamlit Community Cloud.

Stack:
- Auth + data: Supabase (users, bu_submissions, rankings tables)
- File storage: Google Drive (GCP service account)
- Processing: pandas (reads uploaded .xlsx, computes achievement rankings)

Required Supabase schema (create these tables before running the app):

    create table users (
        id uuid primary key default gen_random_uuid(),
        username text unique not null,
        password_hash text not null,       -- bcrypt hash, see hash_password() below
        role text not null check (role in ('admin', 'bu_user')),
        bu_name text,
        created_at timestamptz default now()
    );

    create table bu_submissions (
        id uuid primary key default gen_random_uuid(),
        bu_name text not null,
        period text not null,              -- 'YYYY-MM'
        filename text not null,
        drive_file_id text not null,
        drive_file_link text not null,
        achievement_score numeric not null,
        uploaded_by text not null,
        uploaded_at timestamptz default now()
    );

    create table rankings (
        id uuid primary key default gen_random_uuid(),
        submission_id uuid references bu_submissions(id),
        bu_name text not null,
        period text not null,
        achievement_score numeric not null,
        rank int not null,
        updated_at timestamptz default now(),
        unique (bu_name, period)
    );

To create a user, hash a password locally and insert a row into `users`:
    python -c "import bcrypt; print(bcrypt.hashpw(b'YourPassword123', bcrypt.gensalt()).decode())"
"""

import io
from datetime import datetime, date
from typing import Optional

import pandas as pd
import streamlit as st
from supabase import create_client, Client
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import bcrypt

# ===========================================================================
# Page config & constants
# ===========================================================================
st.set_page_config(
    page_title="BU Performance & Ranking System",
    page_icon="\U0001F4CA",
    layout="wide",
    initial_sidebar_state="expanded",
)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
EXCEL_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_MB = 15

# Column names (case-insensitive) accepted as the "achievement score" for a BU.
ACHIEVEMENT_COLUMN_CANDIDATES = [
    "achievement %",
    "achievement(%)",
    "achievement",
    "achievement score",
    "score",
    "performance %",
]

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
        [data-testid="stDataFrame"] {
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
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["key"]
    except KeyError:
        st.error("Supabase credentials are missing from st.secrets. Please configure `[supabase]` in secrets.toml.")
        st.stop()
    return create_client(url, key)


@st.cache_resource(show_spinner=False)
def get_drive_service():
    try:
        sa_info = dict(st.secrets["gcp_service_account"])
    except KeyError:
        st.error("Google service account credentials are missing from st.secrets. Please configure `[gcp_service_account]`.")
        st.stop()
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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
# Google Drive helpers
# ===========================================================================
def upload_file_to_drive(file_bytes: bytes, filename: str) -> dict:
    service = get_drive_service()
    try:
        folder_id = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]
    except KeyError:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID is missing from st.secrets.")

    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=EXCEL_MIME_TYPE, resumable=True)

    created = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, name, webViewLink")
        .execute()
    )
    return created


# ===========================================================================
# Excel processing
# ===========================================================================
def extract_achievement_score(df: pd.DataFrame) -> float:
    normalized_cols = {str(c).strip().lower(): c for c in df.columns}

    for candidate in ACHIEVEMENT_COLUMN_CANDIDATES:
        if candidate in normalized_cols:
            col = normalized_cols[candidate]
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if not series.empty:
                return float(series.mean())

    raise ValueError(
        "Could not find an achievement/score column in the uploaded file. "
        "Expected a column named one of: " + ", ".join(ACHIEVEMENT_COLUMN_CANDIDATES)
    )


# ===========================================================================
# Supabase data helpers
# ===========================================================================
def save_submission(bu_name: str, period: str, filename: str, drive_file: dict, score: float, uploaded_by: str) -> dict:
    supabase = get_supabase_client()
    record = {
        "bu_name": bu_name,
        "period": period,
        "filename": filename,
        "drive_file_id": drive_file["id"],
        "drive_file_link": drive_file.get("webViewLink", ""),
        "achievement_score": score,
        "uploaded_by": uploaded_by,
    }
    resp = supabase.table("bu_submissions").insert(record).execute()
    return resp.data[0]


def recompute_rankings(period: str) -> list:
    """Recalculate 1st/2nd/... rank for every BU's latest submission in a period."""
    supabase = get_supabase_client()
    resp = supabase.table("bu_submissions").select("*").eq("period", period).execute()
    submissions = resp.data or []

    latest_by_bu = {}
    for s in submissions:
        bu = s["bu_name"]
        if bu not in latest_by_bu or s["uploaded_at"] > latest_by_bu[bu]["uploaded_at"]:
            latest_by_bu[bu] = s

    ranked = sorted(latest_by_bu.values(), key=lambda x: x["achievement_score"], reverse=True)

    for idx, row in enumerate(ranked, start=1):
        supabase.table("rankings").upsert(
            {
                "submission_id": row["id"],
                "bu_name": row["bu_name"],
                "period": period,
                "achievement_score": row["achievement_score"],
                "rank": idx,
            },
            on_conflict="bu_name,period",
        ).execute()

    return ranked


def get_submissions_for_bu(bu_name: str) -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = (
        supabase.table("bu_submissions")
        .select("*")
        .eq("bu_name", bu_name)
        .order("uploaded_at", desc=True)
        .execute()
    )
    return pd.DataFrame(resp.data or [])


def get_all_submissions() -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = supabase.table("bu_submissions").select("*").order("uploaded_at", desc=True).execute()
    return pd.DataFrame(resp.data or [])


def get_rankings_for_period(period: str) -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = supabase.table("rankings").select("*").eq("period", period).order("rank").execute()
    return pd.DataFrame(resp.data or [])


def get_all_rankings() -> pd.DataFrame:
    supabase = get_supabase_client()
    resp = supabase.table("rankings").select("*").order("period", desc=True).order("rank").execute()
    return pd.DataFrame(resp.data or [])


def get_available_periods() -> list:
    df = get_all_submissions()
    if df.empty:
        return []
    return sorted(df["period"].unique().tolist(), reverse=True)


# ===========================================================================
# UI: shared header
# ===========================================================================
def render_app_header(subtitle: str) -> None:
    role_label = "Admin" if st.session_state.get("role") == "admin" else st.session_state.get("bu_name", "")
    st.markdown(
        f"""
        <div class="app-header">
            <div>
                <h1>\U0001F4CA BU Monthly Performance &amp; Ranking System</h1>
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


def render_rank_card(row: Optional[dict], label: str, css_class: str) -> None:
    if row is None:
        st.markdown(
            f"""
            <div class="metric-card {css_class}">
                <div class="label">{label}</div>
                <div class="bu-name">No data yet</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    medal = RANK_MEDALS.get(int(row["rank"]), "\U0001F3C5")
    st.markdown(
        f"""
        <div class="metric-card {css_class}">
            <div class="label">{label}</div>
            <div class="rank-badge">{medal}</div>
            <div class="bu-name">{row['bu_name']}</div>
            <div class="score">{row['achievement_score']:.2f}%</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ===========================================================================
# UI: Login page
# ===========================================================================
def render_login() -> None:
    st.markdown('<div class="login-wrapper">', unsafe_allow_html=True)
    st.markdown("<h2>\U0001F4CA BU Ranking System</h2>", unsafe_allow_html=True)
    st.markdown('<p class="subtitle">Sign in to submit or review monthly performance</p>', unsafe_allow_html=True)

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
            else:
                st.session_state["authenticated"] = True
                st.session_state["role"] = user["role"]
                st.session_state["username"] = user["username"]
                st.session_state["bu_name"] = user.get("bu_name")
                st.session_state["user_id"] = user["id"]
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)


# ===========================================================================
# UI: BU user dashboard
# ===========================================================================
def render_bu_dashboard() -> None:
    render_app_header("Upload your month-end report and track your ranking")
    bu_name = st.session_state["bu_name"]

    if not bu_name:
        st.error("Your account is not linked to a Business Unit. Please contact your admin.")
        return

    col_upload, col_status = st.columns([1.4, 1])

    with col_upload:
        st.markdown("#### \U0001F4E4 Submit Month-End Report")
        today = date.today()
        period_choice = st.date_input("Reporting month", value=today.replace(day=1))
        period = period_choice.strftime("%Y-%m")

        uploaded_file = st.file_uploader("Upload Excel report (.xlsx)", type=["xlsx"])

        if uploaded_file is not None:
            size_mb = uploaded_file.size / (1024 * 1024)
            if size_mb > MAX_UPLOAD_MB:
                st.error(f"File is {size_mb:.1f} MB, which exceeds the {MAX_UPLOAD_MB} MB limit.")
                return

            file_bytes = uploaded_file.getvalue()

            try:
                df_preview = pd.read_excel(io.BytesIO(file_bytes))
            except Exception as exc:
                st.error(f"Could not read the Excel file: {exc}")
                return

            st.markdown("**Preview**")
            st.dataframe(df_preview.head(10), use_container_width=True)

            try:
                score = extract_achievement_score(df_preview)
            except ValueError as exc:
                st.error(str(exc))
                return

            st.info(f"Calculated achievement score for **{period}**: **{score:.2f}%**")

            if st.button("Submit Report", use_container_width=True):
                try:
                    with st.spinner("Uploading to Google Drive..."):
                        drive_file = upload_file_to_drive(file_bytes, uploaded_file.name)

                    with st.spinner("Saving submission..."):
                        save_submission(bu_name, period, uploaded_file.name, drive_file, score, st.session_state["username"])
                        recompute_rankings(period)

                    st.success("Report submitted successfully!")
                    link = drive_file.get("webViewLink", "")
                    if link:
                        st.markdown(f"[\U0001F4C2 View uploaded file on Google Drive]({link})")
                except Exception as exc:
                    st.error(f"Submission failed: {exc}")

    with col_status:
        st.markdown("#### \U0001F3C6 Current Standing")
        rankings_df = get_rankings_for_period(period)
        if rankings_df.empty:
            st.markdown('<div class="info-card">No rankings published for this period yet.</div>', unsafe_allow_html=True)
        else:
            my_row = rankings_df[rankings_df["bu_name"] == bu_name]
            if my_row.empty:
                st.markdown('<div class="info-card">You have not submitted for this period yet.</div>', unsafe_allow_html=True)
            else:
                row = my_row.iloc[0]
                medal = RANK_MEDALS.get(int(row["rank"]), "\U0001F3C5")
                st.markdown(
                    f"""
                    <div class="metric-card">
                        <div class="label">Your Rank ({period})</div>
                        <div class="rank-badge">{medal} #{int(row['rank'])}</div>
                        <div class="score">{row['achievement_score']:.2f}%</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    st.markdown("#### \U0001F4DC Submission History")
    history_df = get_submissions_for_bu(bu_name)
    if history_df.empty:
        st.caption("No submissions yet.")
    else:
        display_cols = ["period", "filename", "achievement_score", "uploaded_at", "drive_file_link"]
        display_cols = [c for c in display_cols if c in history_df.columns]
        st.dataframe(history_df[display_cols], use_container_width=True, hide_index=True)


# ===========================================================================
# UI: Admin dashboard
# ===========================================================================
def render_admin_dashboard() -> None:
    render_app_header("Monitor submissions and BU rankings across the organization")

    periods = get_available_periods()
    if not periods:
        st.info("No submissions have been made yet.")
        return

    selected_period = st.selectbox("Reporting period", periods, index=0)

    rankings_df = get_rankings_for_period(selected_period)

    st.markdown("#### \U0001F3C6 Top Performers")
    col1, col2 = st.columns(2)
    with col1:
        top1 = rankings_df.iloc[0].to_dict() if not rankings_df.empty and len(rankings_df) >= 1 else None
        render_rank_card(top1, "1st Rank", "gold")
    with col2:
        top2 = rankings_df.iloc[1].to_dict() if not rankings_df.empty and len(rankings_df) >= 2 else None
        render_rank_card(top2, "2nd Rank", "silver")

    st.markdown("#### \U0001F4CB Full Ranking Table")
    if rankings_df.empty:
        st.caption("No rankings for this period.")
    else:
        display_df = rankings_df[["rank", "bu_name", "achievement_score", "updated_at"]].copy()
        display_df.columns = ["Rank", "Business Unit", "Achievement %", "Last Updated"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.markdown("#### \U0001F4E5 All Submissions")
    all_submissions_df = get_all_submissions()
    if all_submissions_df.empty:
        st.caption("No submissions recorded yet.")
    else:
        display_cols = ["bu_name", "period", "filename", "achievement_score", "uploaded_by", "uploaded_at", "drive_file_link"]
        display_cols = [c for c in display_cols if c in all_submissions_df.columns]
        st.dataframe(all_submissions_df[display_cols], use_container_width=True, hide_index=True)

    st.markdown("#### \U0001F4E4 Master Ranking Report")
    all_rankings_df = get_all_rankings()
    if all_rankings_df.empty:
        st.caption("Nothing to export yet.")
    else:
        csv_bytes = all_rankings_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Master Ranking Report (CSV)",
            data=csv_bytes,
            file_name=f"master_ranking_report_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )


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
