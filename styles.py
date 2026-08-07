"""
styles.py
---------
Custom CSS injection and small HTML-snippet builders (metric cards, status
badges) for the "Sky Blue" light dashboard theme. Kept separate from app.py
so the visual design can change without touching any business logic.

Colors were picked and contrast-checked deliberately, not eyeballed --
see the :root comment below for the numbers. A literal saturated sky-blue
background would fight readability at this scale, so the page background
is a soft, pale blue tint; the vivid blue is reserved for accents (buttons,
links, highlighted numbers) against white cards, which is what actually
reads as "professional" rather than "loud."
"""

import streamlit as st

# Status -> (text color, tint background) for the color-coded badges.
# Dark, saturated text on a pale tint -- not solid fills -- keeps them
# legible on both the page background and white cards.
STATUS_BADGE_COLORS = {
    "Submitted": ("#15803D", "#DCFCE7"),   # green
    "Late": ("#B91C1C", "#FEE2E2"),         # red
    "Pending": ("#B45309", "#FEF3C7"),      # amber
}


def inject_custom_css() -> None:
    """Sky Blue light theme: pale sky-tinted page background, white cards,
    a deep navy sidebar for chrome/navigation contrast. Also hides
    Streamlit's default chrome (menu, footer, header)."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --bg: #F0F9FF;            /* sky-50 -- soft, pale, not saturated */
            --card: #FFFFFF;
            --card-border: #BAE6FD;   /* sky-200 */
            --text: #0F172A;
            --text-muted: #475569;    /* slate-600 -- 7.1:1 vs bg, safer margin than a lighter gray */
            --sidebar: #0C4A6E;       /* sky-900 -- dark navy chrome, ties into the theme */
            --sidebar-text: #E0F2FE;  /* sky-100 */
            /* Sky blue accent family, split by use so contrast holds at
               every size (validated against --bg and white buttons):
               - accent-large: big bold numbers (>=3:1 floor for large text)
               - accent: buttons, links, small text (>=4.5:1 floor)
               - accent-hover: hover/active state, darkest for clear feedback */
            --accent-large: #0284C7;  /* sky-600 -- 3.8:1 vs bg */
            --accent: #0369A1;        /* sky-700 -- 5.9:1 white-on-it, 5.6:1 vs bg */
            --accent-hover: #075985;  /* sky-800 -- 7.6:1 white-on-it */
            --shadow: 0 1px 3px rgba(2, 132, 199, 0.08), 0 1px 2px rgba(15, 23, 42, 0.04);
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

        h1, h2, h3, h4 {
            font-weight: 700;
            letter-spacing: -0.01em;
        }

        /* Section headers: a consistent, bordered header bar for every
           dashboard section instead of a bare markdown heading */
        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 0.55rem;
            margin: 0.4rem 0 1rem 0;
            border-bottom: 1px solid var(--card-border);
        }
        .section-header .section-title {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            font-size: 0.95rem;
            font-weight: 700;
            color: var(--text);
        }
        .section-header .section-icon {
            width: 26px;
            height: 26px;
            border-radius: 7px;
            background: #E0F2FE;
            color: var(--accent);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            flex-shrink: 0;
        }
        .section-header .section-meta {
            font-size: 0.78rem;
            color: var(--text-muted);
            font-weight: 500;
        }

        /* Empty state */
        .empty-state {
            background: var(--card);
            border: 1px dashed var(--card-border);
            border-radius: 10px;
            padding: 1.5rem;
            text-align: center;
            color: var(--text-muted);
            font-size: 0.85rem;
        }

        /* KPI / metric cards */
        .metric-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 1.2rem 1.4rem;
            box-shadow: var(--shadow);
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
        .metric-card.accent .metric-value { color: var(--accent-large); }
        .metric-card.danger .metric-value { color: #B91C1C; }
        .metric-card.warn .metric-value { color: #B45309; }

        /* Status badges */
        .status-badge {
            display: inline-block;
            padding: 3px 11px;
            border-radius: 999px;
            font-size: 0.75rem;
            font-weight: 700;
        }

        /* Generic card wrapper for sections */
        .dark-card {
            background: var(--card);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 1.2rem 1.4rem;
            box-shadow: var(--shadow);
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
        .stTextInput input:focus, .stNumberInput input:focus {
            border-color: var(--accent) !important;
            box-shadow: 0 0 0 3px rgba(3, 105, 161, 0.15) !important;
        }

        /* Tables */
        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--card-border);
            box-shadow: var(--shadow);
        }

        /* Tabs -- pill-style so they read as a segmented control */
        [data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 4px;
            background: #E0F2FE;
            padding: 4px;
            border-radius: 10px;
            border: 1px solid var(--card-border);
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
            color: var(--text-muted);
            font-weight: 600;
            font-size: 0.85rem;
        }
        [data-testid="stTabs"] button[aria-selected="true"] {
            background: var(--card);
            color: var(--accent);
            box-shadow: var(--shadow);
        }

        [data-testid="stSidebar"] {
            background: var(--sidebar);
            border-right: 1px solid var(--card-border);
        }
        [data-testid="stSidebar"] * {
            color: var(--sidebar-text) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_badge_html(status: str) -> str:
    color, bg = STATUS_BADGE_COLORS.get(status, ("#475569", "#F1F5F9"))
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


def section_header_html(icon: str, title: str, meta: str = "") -> str:
    meta_html = f'<span class="section-meta">{meta}</span>' if meta else ""
    return f"""
        <div class="section-header">
            <div class="section-title"><span class="section-icon">{icon}</span>{title}</div>
            {meta_html}
        </div>
    """


def empty_state_html(message: str) -> str:
    return f'<div class="empty-state">{message}</div>'
