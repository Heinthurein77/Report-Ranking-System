"""
styles.py
---------
Custom CSS injection and small HTML-snippet builders (metric cards, status
badges) for the Dark Slate dashboard theme. Kept separate from app.py so the
visual design can change without touching any business logic.
"""

import streamlit as st

# Status -> (label color, background) for the color-coded badges.
STATUS_BADGE_COLORS = {
    "Submitted": ("#22C55E", "rgba(34, 197, 94, 0.12)"),   # green
    "Late": ("#EF4444", "rgba(239, 68, 68, 0.12)"),         # red
    "Pending": ("#EAB308", "rgba(234, 179, 8, 0.12)"),      # yellow
}


def inject_custom_css() -> None:
    """Dark Slate theme: #0F172A page background, #1E293B cards, rounded
    borders. Also hides Streamlit's default chrome (menu, footer, header)."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --bg: #0F172A;
            --card: #1E293B;
            --card-border: #334155;
            --text: #F1F5F9;
            --text-muted: #94A3B8;
            --accent: #6366F1;
            --accent-hover: #4F46E5;
        }

        html, body, [class*="css"] {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }

        .stApp {
            background: var(--bg);
            color: var(--text);
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

        h1, h2, h3, h4, h5, p, span, label, div {
            color: var(--text);
        }

        /* KPI / metric cards */
        .metric-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 1.2rem 1.4rem;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.25);
            height: 100%;
        }
        .metric-card .metric-label {
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .metric-card .metric-value {
            font-size: 1.9rem;
            font-weight: 800;
            color: var(--text);
            margin-top: 0.3rem;
        }
        .metric-card .metric-sub {
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-top: 0.2rem;
        }
        .metric-card.accent .metric-value { color: var(--accent); }
        .metric-card.danger .metric-value { color: #EF4444; }
        .metric-card.warn .metric-value { color: #EAB308; }

        /* Status badges */
        .status-badge {
            display: inline-block;
            padding: 3px 11px;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 700;
        }

        /* Generic dark card wrapper for sections */
        .dark-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 1.2rem 1.4rem;
        }

        /* Buttons */
        .stButton > button, .stDownloadButton > button {
            border-radius: 8px;
            font-weight: 600;
            border: 1px solid var(--accent);
            background: var(--accent);
            color: #fff;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            background: var(--accent-hover);
            border-color: var(--accent-hover);
            color: #fff;
        }

        /* Inputs */
        .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
            background: var(--card) !important;
            color: var(--text) !important;
            border-radius: 8px !important;
            border: 1px solid var(--card-border) !important;
        }

        /* Tables */
        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--card-border);
        }

        [data-testid="stSidebar"] {
            background: #0B1120;
            border-right: 1px solid var(--card-border);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_badge_html(status: str) -> str:
    color, bg = STATUS_BADGE_COLORS.get(status, ("#94A3B8", "rgba(148, 163, 184, 0.12)"))
    return f'<span class="status-badge" style="color:{color};background:{bg};">{status}</span>'


def metric_card_html(label: str, value: str, sub: str = "", variant: str = "") -> str:
    css_class = f"metric-card {variant}".strip()
    sub_html = f'<div class="metric-sub">{sub}</div>' if sub else ""
    return f"""
        <div class="{css_class}">
            <div class="metric-label">{label}</div>
            <div class="metric-value">{value}</div>
            {sub_html}
        </div>
    """
