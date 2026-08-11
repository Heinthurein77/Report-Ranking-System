"""
ranking_mcp_server.py
----------------------
An MCP (Model Context Protocol) server, built on FastMCP, that lets Claude
Desktop query the BU Monthly Performance & Ranking System directly against
its live Supabase project.

SCHEMA THIS TARGETS (the actual deployed schema -- see schema.sql/db.py in
the main app, not a hypothetical one):

    business_units
        id (uuid), bu_name, bu_code, created_at

    monthly_reports
        id (uuid), bu_id (uuid -> business_units.id), month_year ('YYYY-MM'),
        file_name, file_url, file_path, rank (int, nullable),
        submitted_at (timestamptz, UTC), status ('Submitted'|'Late'|'Pending'),
        submitted_by (uuid -> profiles.id)

    Storage bucket "bu-reports" (public), path layout:
        {bu_name_lowercased_and_sanitized}/{BuName}_{month_year}.{ext}
    e.g. "sales/Sales_2026-08.xlsx" -- see db.py: upload_report_file().

WHY THE SERVICE-ROLE KEY, NOT THE ANON KEY:
    Row Level Security on monthly_reports only lets a non-admin session see
    rows for its OWN bu_id (this is precisely the bug that made every BU
    get rank 1 in the main app, before it was fixed to use the service
    client for the cross-BU count). This server is a standalone local tool
    for the person operating Claude Desktop -- it always needs full
    visibility across every BU, so it must run with the service-role key
    (SUPABASE_KEY below), never the publishable/anon key.

TOOLS:
    get_submission_rankings(submission_month=None)
        Markdown table of rank/status/file per BU, optionally filtered to
        one 'YYYY-MM' period.

    read_bu_submission_file(bu_name, submission_month, sheet_max_rows=50)
        Locates that BU's report for that month, downloads it from
        Storage, and returns a markdown preview of every sheet (Excel) or
        the single table (CSV) -- shape (rows/columns) plus a data preview
        capped at sheet_max_rows per sheet.

See the bottom of this file for install + Claude Desktop config steps.
"""

import io
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from supabase import Client, create_client

from mcp.server.fastmcp import FastMCP

# ============================================================================
# Configuration (all via environment variables -- see claude_desktop_config.json below)
# ============================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service-role key -- see note above
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "bu-reports")

# Myanmar Standard Time -- fixed offset, no DST. Matches the main app's
# convention (ranking.py) so timestamps read the same way in both places.
MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))

EXCEL_EXTENSIONS = {".xlsx", ".xls"}
CSV_EXTENSIONS = {".csv"}

mcp = FastMCP("bu-ranking-system")


# ============================================================================
# Supabase client
# ============================================================================
_client: Optional[Client] = None


def get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set as environment variables "
                "(see claude_desktop_config.json setup at the bottom of this file)."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ============================================================================
# Small helpers
# ============================================================================
def format_mmt(timestamp) -> str:
    """Convert a Supabase UTC timestamptz string to a Myanmar-time display
    string. Defensive against None/NaN/non-string input, same as the main
    app's ranking.format_mmt()."""
    if not isinstance(timestamp, str) or not timestamp:
        return "—"
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYANMAR_TZ).strftime("%Y-%m-%d %H:%M") + " MMT"


def clean_label(text) -> str:
    """A cell value OR a column name can contain a literal newline in a
    real-world spreadsheet (seen live: a Burmese-language header wrapped
    across two lines in the source Excel file) -- either one breaks a
    markdown table row if left as-is, so every piece of text going into a
    table (headers included, not just cell values) goes through this."""
    return str(text).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def rows_to_markdown(rows: list, columns: list) -> str:
    """Renders a list of dicts as a markdown table without depending on the
    optional `tabulate` package. `columns` also controls display order."""
    if not rows:
        return "_No matching rows found._"
    clean_columns = [clean_label(col) for col in columns]
    header = "| " + " | ".join(clean_columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines = [header, separator]
    for row in rows:
        cells = [clean_label(row.get(col, "")) for col in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int) -> str:
    preview = df.head(max_rows)
    columns = [str(c) for c in preview.columns]
    rows = [dict(zip(columns, [row[c] for c in preview.columns])) for _, row in preview.iterrows()]
    return rows_to_markdown(rows, columns)


def find_business_unit(client: Client, bu_name: str) -> Optional[dict]:
    """Case-insensitive match, same convention as the main app's
    get_or_create_business_unit(), so "Sales" and "sales" resolve to the
    same BU here too."""
    resp = client.table("business_units").select("id, bu_name, bu_code").ilike("bu_name", bu_name.strip()).limit(1).execute()
    return resp.data[0] if resp.data else None


def find_report(client: Client, bu_id: str, month_year: str) -> Optional[dict]:
    resp = (
        client.table("monthly_reports")
        .select("id, file_name, file_url, file_path, rank, status, submitted_at")
        .eq("bu_id", bu_id)
        .eq("month_year", month_year)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def download_file_bytes(client: Client, file_path: str, file_url: str) -> bytes:
    """Storage API first (works regardless of bucket public/private
    status); fall back to the public file_url over plain HTTP if that
    fails for any reason (e.g. a stale/mismatched file_path)."""
    if file_path:
        try:
            return client.storage.from_(SUPABASE_BUCKET).download(file_path)
        except Exception:
            pass

    if file_url:
        import httpx

        response = httpx.get(file_url, timeout=30.0)
        response.raise_for_status()
        return response.content

    raise FileNotFoundError("Report has neither a file_path nor a file_url to download from.")


# ============================================================================
# Tool 1: get_submission_rankings
# ============================================================================
@mcp.tool()
def get_submission_rankings(submission_month: Optional[str] = None) -> str:
    """Query the BU ranking system and return a markdown table of
    submissions and their ranks.

    Args:
        submission_month: Optional 'YYYY-MM' period to filter to (e.g.
            '2026-03'). If omitted, returns every submission across all
            months, most recent first.

    Returns:
        A markdown table with columns: Month, Business Unit, Rank, Status,
        File, Submitted At (Myanmar time). BUs that haven't submitted yet
        for a given month are NOT included (this queries actual
        submissions, not the full BU roster) -- ask for a specific month
        to see who's missing by comparing against the BU list separately
        if needed.
    """
    client = get_client()

    query = (
        client.table("monthly_reports")
        .select("month_year, rank, status, file_name, submitted_at, business_units(bu_name)")
    )
    if submission_month:
        query = query.eq("month_year", submission_month)
    resp = query.order("month_year", desc=True).order("rank").execute()

    rows = []
    for record in resp.data or []:
        bu_info = record.get("business_units") or {}
        rows.append(
            {
                "Month": record.get("month_year", "—"),
                "Business Unit": bu_info.get("bu_name", "—"),
                "Rank": record["rank"] if record.get("rank") is not None else "—",
                "Status": record.get("status", "—"),
                "File": record.get("file_name", "—"),
                "Submitted At (MMT)": format_mmt(record.get("submitted_at")),
            }
        )

    columns = ["Month", "Business Unit", "Rank", "Status", "File", "Submitted At (MMT)"]
    title = f"### Submission Rankings — {submission_month}" if submission_month else "### Submission Rankings — All Months"
    return f"{title}\n\n{rows_to_markdown(rows, columns)}"


# ============================================================================
# Tool 2: read_bu_submission_file
# ============================================================================
@mcp.tool()
def read_bu_submission_file(bu_name: str, submission_month: str, sheet_max_rows: int = 50) -> str:
    """Locate a specific Business Unit's report for a given month, download
    it from Supabase Storage, and return a markdown preview of its
    contents. Handles multi-sheet Excel (.xlsx/.xls) by previewing every
    sheet separately, and single-table CSV files.

    Args:
        bu_name: Business Unit name (case-insensitive, e.g. 'Sales').
        submission_month: 'YYYY-MM' period the report was submitted for.
        sheet_max_rows: Max rows to preview per sheet (default 50). The
            full row/column count is always reported even if the preview
            is truncated.

    Returns:
        Markdown: file metadata, then for each sheet its row count, column
        list, and a data preview table.
    """
    client = get_client()

    business_unit = find_business_unit(client, bu_name)
    if not business_unit:
        return f"No Business Unit found matching '{bu_name}'."

    report = find_report(client, business_unit["id"], submission_month)
    if not report:
        return f"No submission found for '{business_unit['bu_name']}' in {submission_month}."

    file_name = report.get("file_name") or ""
    ext = os.path.splitext(file_name)[1].lower()

    try:
        content = download_file_bytes(client, report.get("file_path", ""), report.get("file_url", ""))
    except Exception as exc:
        return f"Found the submission record for '{business_unit['bu_name']}' ({file_name}) but could not download it: {exc}"

    header = (
        f"### {business_unit['bu_name']} — {submission_month}\n\n"
        f"**File:** {file_name}  \n"
        f"**Rank:** {report.get('rank', '—')}  \n"
        f"**Status:** {report.get('status', '—')}  \n"
        f"**Submitted At:** {format_mmt(report.get('submitted_at'))}\n"
    )

    if ext in EXCEL_EXTENSIONS:
        try:
            engine = "openpyxl" if ext == ".xlsx" else "xlrd"
            sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, engine=engine)
        except Exception as exc:
            return header + f"\n_Could not parse as Excel: {exc}_"

        if not sheets:
            return header + "\n_The workbook has no sheets._"

        sections = [header, f"\n**{len(sheets)} sheet(s) found.**\n"]
        for sheet_name, df in sheets.items():
            sections.append(
                f"\n#### Sheet: `{sheet_name}`\n"
                f"Rows: {len(df)} · Columns: {len(df.columns)}\n\n"
                f"Columns: {', '.join(clean_label(c) for c in df.columns)}\n\n"
                f"{dataframe_to_markdown(df, sheet_max_rows)}\n"
            )
            if len(df) > sheet_max_rows:
                sections.append(f"\n_...{len(df) - sheet_max_rows} more row(s) not shown._\n")
        return "".join(sections)

    if ext in CSV_EXTENSIONS:
        try:
            df = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            return header + f"\n_Could not parse as CSV: {exc}_"

        result = (
            header
            + f"\nRows: {len(df)} · Columns: {len(df.columns)}\n\n"
            + f"Columns: {', '.join(clean_label(c) for c in df.columns)}\n\n"
            + dataframe_to_markdown(df, sheet_max_rows)
        )
        if len(df) > sheet_max_rows:
            result += f"\n\n_...{len(df) - sheet_max_rows} more row(s) not shown._"
        return result

    return header + f"\n_Unsupported file type '{ext}' -- only .xlsx, .xls, and .csv are previewed._"


# ============================================================================
# Entrypoint (stdio transport -- what Claude Desktop expects)
# ============================================================================
if __name__ == "__main__":
    mcp.run()


# ============================================================================
# SETUP
# ============================================================================
#
# 1. Install dependencies:
#
#      pip install -r requirements-mcp.txt
#
#    or directly:
#
#      pip install "mcp[cli]" supabase pandas openpyxl xlrd httpx
#
# 2. Add this server to Claude Desktop's config file:
#      - Windows: %APPDATA%\Claude\claude_desktop_config.json
#      - macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json
#
#    {
#      "mcpServers": {
#        "bu-ranking-system": {
#          "command": "python",
#          "args": ["C:\\absolute\\path\\to\\ranking_mcp_server.py"],
#          "env": {
#            "SUPABASE_URL": "https://your-project.supabase.co",
#            "SUPABASE_KEY": "sb_secret_xxxxxxxxxxxxxxxxxxxxxxxx",
#            "SUPABASE_BUCKET": "bu-reports"
#          }
#        }
#      }
#    }
#
#    Use an ABSOLUTE path to this script in "args". SUPABASE_KEY must be
#    the SERVICE-ROLE ("secret") key, not the publishable/anon key -- see
#    the module docstring above for why.
#
# 3. Fully quit and reopen Claude Desktop (a reload isn't enough for MCP
#    server config changes to take effect).
#
# 4. Test it by asking Claude Desktop something like:
#      "Show me the submission rankings for 2026-08"
#      "Read the Sales BU's report for 2026-08 and summarize each sheet"
