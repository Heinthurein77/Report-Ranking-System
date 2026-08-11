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

TOOLS -- read:
    get_submission_rankings(submission_month=None)
        Markdown table of rank/status/file per BU, optionally filtered to
        one 'YYYY-MM' period. Only shows BUs that HAVE submitted.

    read_bu_submission_file(bu_name, submission_month, sheet_max_rows=50)
        Locates that BU's report for that month, downloads it from
        Storage, and returns a markdown preview of every sheet (Excel) or
        the single table (CSV) -- shape (rows/columns) plus a data preview
        capped at sheet_max_rows per sheet.

    list_business_units()
        The full BU roster with submission counts -- the reference list to
        compare submissions against.

    get_missing_submissions(submission_month)
        Roster minus submitters: who still owes a report for that month.

    get_bu_history(bu_name)
        One BU's rank/status/timing across every month, with a summary.

    validate_submission(bu_name, submission_month, required_sheets=None,
                        required_columns=None)
        Downloads the file and checks it is actually usable -- parses,
        has data rows, no blank/unnamed columns, required sheets and
        columns present. The app stores uploads unparsed, so "Submitted"
        alone says nothing about the contents.

    get_ranking_criteria()
        Explains how rank and status are decided (and the limits of
        ranking by arrival order), so a rank can be justified to a BU.

    compare_bus(submission_month)
        Full side-by-side for one month: every BU, submitted and missing
        together, with margin vs the deadline.

    get_deadline_status(submission_month=None)
        Time remaining/overdue for the month, plus the on-time / late /
        not-submitted split. Defaults to the current MMT month.

    draft_reminder_message(submission_month=None, language='both')
        Composes a reminder for the BUs that haven't submitted, in
        English and/or Burmese. Drafts only -- sends nothing.

TOOLS -- write (these MUTATE the live database and Storage):
    submit_bu_report(bu_name, submission_month, local_file_path,
                     overwrite=False)
        Uploads a local file to Storage under the same naming scheme the
        Streamlit app uses, assigns the next arrival rank, derives
        Submitted/Late from the deadline, and inserts the record.
        submitted_by is left null -- this server acts as the operator,
        not as a signed-in user account. One report per BU per month, so
        replacing one requires overwrite=True (original rank is kept).

    set_report_rank_status(bu_name, submission_month, rank=None,
                           status=None, resequence=True)
        Admin override for rank and/or status. A rank change re-inserts
        the BU at that position and renumbers the month to a contiguous
        1..N rather than leaving duplicate ranks behind.

    Both bypass RLS entirely (service-role key), so there is no
    is_admin() check standing between a tool call and the data -- whoever
    can call this server can write as admin. That is the same trust level
    as handing someone the service key; keep the config file private.

See the bottom of this file for install + Claude Desktop config steps.
"""

import io
import mimetypes
import os
import re
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

# Submission deadline: the 14th of the reporting month, 23:59:59 MMT. Kept in
# sync with ranking.DEADLINE_DAY in the main app -- if you change it there,
# change it here (it's a code constant in both places, not a DB setting).
DEADLINE_DAY = 14

VALID_STATUSES = ("Submitted", "Late", "Pending")

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
# Deadline / status helpers -- mirrors ranking.py in the main app so the MCP
# server and the Streamlit UI never disagree about what counts as "Late".
# ============================================================================
def now_mmt() -> datetime:
    return datetime.now(MYANMAR_TZ)


def parse_month(month_year: str) -> tuple:
    """Validate a 'YYYY-MM' period string and return (year, month). Raises
    ValueError with a usable message -- every tool that takes a month runs
    its input through this so a typo fails loudly here rather than silently
    matching zero rows further down."""
    try:
        year_part, month_part = str(month_year).strip().split("-")
        year, month = int(year_part), int(month_part)
    except (ValueError, AttributeError):
        raise ValueError(f"'{month_year}' is not a valid period -- use 'YYYY-MM', e.g. '2026-08'.")
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        raise ValueError(f"'{month_year}' is not a valid period -- use 'YYYY-MM', e.g. '2026-08'.")
    return year, month


def get_deadline(month_year: str) -> datetime:
    """23:59:59 MMT on the 14th of the given 'YYYY-MM' period."""
    year, month = parse_month(month_year)
    return datetime(year, month, DEADLINE_DAY, 23, 59, 59, tzinfo=MYANMAR_TZ)


def determine_status(submitted_at: datetime, month_year: str) -> str:
    """'Late' if submitted after the deadline, else 'Submitted'. Same rule as
    ranking.determine_status()."""
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    return "Late" if submitted_at.astimezone(MYANMAR_TZ) > get_deadline(month_year) else "Submitted"


def parse_to_mmt(timestamp) -> Optional[datetime]:
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MYANMAR_TZ)


def describe_margin(submitted_at, month_year: str) -> str:
    """How far before/after the deadline a report landed, as a readable
    string -- the number managers actually care about, which a bare
    'Late' status doesn't convey."""
    dt = parse_to_mmt(submitted_at)
    if dt is None:
        return "—"
    delta = dt - get_deadline(month_year)
    hours = abs(delta.total_seconds()) / 3600
    unit = f"{hours / 24:.1f} day(s)" if hours >= 24 else f"{hours:.1f} hour(s)"
    return f"{unit} late" if delta.total_seconds() > 0 else f"{unit} early"


# ============================================================================
# Roster / report query helpers
# ============================================================================
def list_all_business_units(client: Client) -> list:
    resp = (
        client.table("business_units")
        .select("id, bu_name, bu_code, created_at")
        .order("bu_name")
        .execute()
    )
    return resp.data or []


def reports_for_month(client: Client, month_year: str) -> list:
    """Every report for one month, with the BU name joined in. Ordered by
    rank, with unranked rows last (Postgrest puts NULLs last by default on
    an ascending order)."""
    resp = (
        client.table("monthly_reports")
        .select(
            "id, bu_id, month_year, file_name, file_url, file_path, rank, status, "
            "submitted_at, business_units(bu_name)"
        )
        .eq("month_year", month_year)
        .order("rank")
        .execute()
    )
    return resp.data or []


def next_rank(client: Client, month_year: str) -> int:
    """Arrival order within the month: 1st BU to submit gets rank 1. Uses the
    service-role client (as this whole server does) for the true cross-BU
    count -- see the module docstring for why an RLS-scoped session client
    would return 0 here and hand every BU rank 1."""
    resp = (
        client.table("monthly_reports")
        .select("id", count="exact")
        .eq("month_year", month_year)
        .execute()
    )
    return (resp.count or 0) + 1


def resequence_month(client: Client, month_year: str) -> int:
    """Rewrite every rank for the month as a contiguous 1..N in the current
    rank order, breaking ties by submission time. Closes the gaps a delete
    leaves behind and the duplicates a manual rank edit creates. Returns the
    number of rows actually changed."""
    rows = reports_for_month(client, month_year)
    ordered = sorted(
        rows,
        key=lambda r: (
            r.get("rank") is None,
            r.get("rank") if r.get("rank") is not None else 0,
            r.get("submitted_at") or "",
        ),
    )
    return write_ranks(client, ordered)


def write_ranks(client: Client, ordered_rows: list) -> int:
    """Persist 1..N against an already-ordered list of report rows, skipping
    rows whose rank is already correct. Returns how many were changed."""
    changed = 0
    for position, row in enumerate(ordered_rows, start=1):
        if row.get("rank") != position:
            client.table("monthly_reports").update({"rank": position}).eq("id", row["id"]).execute()
            changed += 1
    return changed


def move_to_rank(client: Client, month_year: str, report_id: str, new_rank: int) -> int:
    """Move one report to a specific position and renumber the month 1..N
    around it.

    A plain UPDATE to an occupied rank would leave two BUs sharing it, so
    the row is pulled out of the month's current order and re-inserted at
    the requested index instead -- everything below it shifts down by one,
    the way a manual reordering is expected to behave. Returns how many rows
    were renumbered."""
    rows = reports_for_month(client, month_year)
    ordered = sorted(
        rows,
        key=lambda r: (
            r.get("rank") is None,
            r.get("rank") if r.get("rank") is not None else 0,
            r.get("submitted_at") or "",
        ),
    )
    target = next((r for r in ordered if r["id"] == report_id), None)
    if target is None:
        return 0

    remaining = [r for r in ordered if r["id"] != report_id]
    index = max(0, min(new_rank - 1, len(remaining)))
    remaining.insert(index, target)
    return write_ranks(client, remaining)


def storage_names_for(bu_name: str, month_year: str, ext: str) -> tuple:
    """Reproduces db.upload_report_file()'s naming exactly, so files written
    by this server land in the same place the Streamlit app would put them:
        Sales, 2026-08, .xlsx -> ("Sales_2026-08.xlsx", "sales/Sales_2026-08.xlsx")
    """
    clean = re.sub(r"[^A-Za-z0-9]+", "_", bu_name.strip()).strip("_") or "bu"
    display_name = f"{clean}_{month_year}{ext}"
    return display_name, f"{clean.lower()}/{display_name}"


def split_csv_arg(value: Optional[str]) -> list:
    """Several tools take an optional comma-separated list. MCP clients vary
    in how reliably they send real arrays, so these arrive as one string and
    are split here."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


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
# Tool 3: list_business_units
# ============================================================================
@mcp.tool()
def list_business_units() -> str:
    """List every Business Unit registered in the system -- the full roster,
    regardless of whether they have ever submitted a report.

    get_submission_rankings only shows BUs that HAVE submitted, so this is
    the reference list to compare against when working out who is missing.

    Returns:
        A markdown table: Business Unit, Code, Submissions (total count),
        Latest Month submitted, Registered date (Myanmar time).
    """
    client = get_client()

    business_units = list_all_business_units(client)
    if not business_units:
        return "_No Business Units are registered yet._"

    resp = client.table("monthly_reports").select("bu_id, month_year").execute()
    counts: dict = {}
    latest: dict = {}
    for record in resp.data or []:
        bu_id = record.get("bu_id")
        counts[bu_id] = counts.get(bu_id, 0) + 1
        month = record.get("month_year") or ""
        if month > latest.get(bu_id, ""):
            latest[bu_id] = month

    rows = [
        {
            "Business Unit": bu.get("bu_name", "—"),
            "Code": bu.get("bu_code", "—"),
            "Submissions": counts.get(bu["id"], 0),
            "Latest Month": latest.get(bu["id"], "—"),
            "Registered (MMT)": format_mmt(bu.get("created_at")),
        }
        for bu in business_units
    ]

    columns = ["Business Unit", "Code", "Submissions", "Latest Month", "Registered (MMT)"]
    return (
        f"### Business Units — {len(business_units)} registered\n\n"
        f"{rows_to_markdown(rows, columns)}"
    )


# ============================================================================
# Tool 4: get_missing_submissions
# ============================================================================
@mcp.tool()
def get_missing_submissions(submission_month: str) -> str:
    """Which Business Units have NOT submitted a report for a given month.

    This is the roster (business_units) minus the BUs that have a row in
    monthly_reports for that period -- the question get_submission_rankings
    structurally cannot answer, since it only sees actual submissions.

    Args:
        submission_month: 'YYYY-MM' period to check (e.g. '2026-08').

    Returns:
        Markdown: a submitted/missing count, the deadline and whether it has
        passed, and a table of the BUs still outstanding.
    """
    client = get_client()

    try:
        deadline = get_deadline(submission_month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    business_units = list_all_business_units(client)
    if not business_units:
        return "_No Business Units are registered yet, so nothing can be missing._"

    submitted_ids = {r["bu_id"] for r in reports_for_month(client, submission_month)}
    missing = [bu for bu in business_units if bu["id"] not in submitted_ids]

    now = now_mmt()
    passed = now > deadline
    deadline_line = (
        f"**Deadline:** {deadline.strftime('%Y-%m-%d %H:%M')} MMT — "
        + ("**passed**" if passed else f"{(deadline - now).days} day(s) remaining")
    )

    header = (
        f"### Missing Submissions — {submission_month}\n\n"
        f"{len(submitted_ids)} of {len(business_units)} submitted · "
        f"**{len(missing)} outstanding**  \n"
        f"{deadline_line}\n\n"
    )

    if not missing:
        return header + "_Every Business Unit has submitted for this month._"

    rows = [{"Business Unit": bu.get("bu_name", "—"), "Code": bu.get("bu_code", "—")} for bu in missing]
    return header + rows_to_markdown(rows, ["Business Unit", "Code"])


# ============================================================================
# Tool 5: submit_bu_report  (WRITE)
# ============================================================================
@mcp.tool()
def submit_bu_report(
    bu_name: str,
    submission_month: str,
    local_file_path: str,
    overwrite: bool = False,
) -> str:
    """Upload a report file for a Business Unit and record the submission --
    the write counterpart to read_bu_submission_file.

    Reads the file from the local machine running this server, uploads it to
    Supabase Storage under the same naming scheme the Streamlit app uses
    ({bu}/{Bu}_{YYYY-MM}.{ext}), assigns the next arrival rank for that
    month, derives Submitted/Late from the deadline, and inserts the
    monthly_reports row. `submitted_by` is left null -- this server acts as
    the operator, not as any particular signed-in user account.

    The schema allows only ONE report per BU per month, so re-submitting
    requires overwrite=True, which replaces both the stored file and the
    record while KEEPING the original rank (a correction should not cost the
    BU its arrival position).

    Args:
        bu_name: Business Unit name (case-insensitive; must already exist --
            BUs are created at registration, not here).
        submission_month: 'YYYY-MM' period the report is for.
        local_file_path: Absolute path to the file on this machine.
        overwrite: Replace an existing submission for this BU/month.

    Returns:
        Markdown confirmation with the assigned rank, status, storage path,
        and public URL.
    """
    client = get_client()

    try:
        parse_month(submission_month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    if not os.path.isfile(local_file_path):
        return f"**No such file:** `{local_file_path}` (pass an absolute path on this machine)."

    business_unit = find_business_unit(client, bu_name)
    if not business_unit:
        return (
            f"No Business Unit found matching '{bu_name}'. Business Units are created when a user "
            f"registers -- run list_business_units() to see the exact names in use."
        )

    existing = find_report(client, business_unit["id"], submission_month)
    if existing and not overwrite:
        return (
            f"**'{business_unit['bu_name']}' has already submitted for {submission_month}** "
            f"(rank {existing.get('rank', '—')}, {existing.get('status', '—')}, "
            f"file `{existing.get('file_name', '—')}`).\n\n"
            f"The schema permits one report per BU per month. Call again with overwrite=true to "
            f"replace it (the original rank is kept)."
        )

    ext = os.path.splitext(local_file_path)[1].lower() or ".xlsx"
    display_name, storage_path = storage_names_for(business_unit["bu_name"], submission_month, ext)
    content_type = mimetypes.guess_type(local_file_path)[0] or "application/octet-stream"

    with open(local_file_path, "rb") as handle:
        file_bytes = handle.read()

    try:
        client.storage.from_(SUPABASE_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            # Storage rejects an upload to an existing path unless upsert is
            # set; supabase-py passes these options through as HTTP headers,
            # so the value must be the STRING "true", not a bool.
            file_options={"content-type": content_type, "upsert": "true"},
        )
    except Exception as exc:
        return f"**Upload failed** for `{storage_path}`: {exc}"

    public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)
    submitted_at = now_mmt()
    status = determine_status(submitted_at, submission_month)

    payload = {
        "file_name": display_name,
        "file_url": public_url,
        "file_path": storage_path,
        "status": status,
    }

    try:
        if existing:
            rank = existing.get("rank")
            client.table("monthly_reports").update(payload).eq("id", existing["id"]).execute()
            action = "Replaced"
        else:
            rank = next_rank(client, submission_month)
            payload.update(
                {
                    "bu_id": business_unit["id"],
                    "month_year": submission_month,
                    "rank": rank,
                    "submitted_at": submitted_at.astimezone(timezone.utc).isoformat(),
                }
            )
            client.table("monthly_reports").insert(payload).execute()
            action = "Submitted"
    except Exception as exc:
        return f"**File uploaded, but the database record failed:** {exc}"

    return (
        f"### {action} — {business_unit['bu_name']} · {submission_month}\n\n"
        f"**Rank:** {rank}  \n"
        f"**Status:** {status} ({describe_margin(submitted_at.isoformat(), submission_month)})  \n"
        f"**File:** {display_name} ({len(file_bytes):,} bytes)  \n"
        f"**Storage path:** `{storage_path}`  \n"
        f"**URL:** {public_url}  \n"
        f"**Submitted at:** {submitted_at.strftime('%Y-%m-%d %H:%M')} MMT"
    )


# ============================================================================
# Tool 6: set_report_rank_status  (WRITE)
# ============================================================================
@mcp.tool()
def set_report_rank_status(
    bu_name: str,
    submission_month: str,
    rank: Optional[int] = None,
    status: Optional[str] = None,
    resequence: bool = True,
) -> str:
    """Correct a submission's rank and/or status -- the admin override path.

    Rank is normally arrival order, assigned automatically at submission.
    Use this when that needs adjusting (a BU submitted by email first, a
    file was rejected and resubmitted, etc.).

    Moving a BU to a rank another BU already holds would leave duplicates,
    so by default the whole month is resequenced afterward into a contiguous
    1..N with the moved BU at its new position. Pass resequence=false to
    write the raw value untouched.

    Args:
        bu_name: Business Unit name (case-insensitive).
        submission_month: 'YYYY-MM' period.
        rank: New rank (1 = first). Omit to leave unchanged.
        status: One of 'Submitted', 'Late', 'Pending'. Omit to leave
            unchanged. (These are the only values the schema's CHECK
            constraint accepts.)
        resequence: Renumber the month to a contiguous 1..N afterward.

    Returns:
        Markdown confirmation, plus the month's resulting rank order.
    """
    client = get_client()

    if rank is None and status is None:
        return "**Nothing to do** -- pass a rank, a status, or both."

    if status is not None and status not in VALID_STATUSES:
        return f"**Invalid status** '{status}'. The schema allows only: {', '.join(VALID_STATUSES)}."

    if rank is not None and rank < 1:
        return "**Invalid rank** -- rank starts at 1."

    try:
        parse_month(submission_month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    business_unit = find_business_unit(client, bu_name)
    if not business_unit:
        return f"No Business Unit found matching '{bu_name}'."

    report = find_report(client, business_unit["id"], submission_month)
    if not report:
        return f"No submission found for '{business_unit['bu_name']}' in {submission_month}."

    before = f"rank {report.get('rank', '—')}, {report.get('status', '—')}"

    try:
        if status is not None:
            client.table("monthly_reports").update({"status": status}).eq("id", report["id"]).execute()

        note = ""
        if rank is not None:
            if resequence:
                changed = move_to_rank(client, submission_month, report["id"], rank)
                note = (
                    f"\n\nMoved to position {rank} and renumbered the month "
                    f"— {changed} row(s) changed, ranks stay contiguous 1..N."
                )
            else:
                client.table("monthly_reports").update({"rank": rank}).eq("id", report["id"]).execute()
                note = "\n\n_resequence=false — the raw rank was written; duplicates are possible._"
    except Exception as exc:
        return f"**Update failed:** {exc}"

    after_rows = reports_for_month(client, submission_month)
    updated = next((r for r in after_rows if r["id"] == report["id"]), report)
    table = rows_to_markdown(
        [
            {
                "Rank": r["rank"] if r.get("rank") is not None else "—",
                "Business Unit": (r.get("business_units") or {}).get("bu_name", "—"),
                "Status": r.get("status", "—"),
            }
            for r in after_rows
        ],
        ["Rank", "Business Unit", "Status"],
    )

    return (
        f"### Updated — {business_unit['bu_name']} · {submission_month}\n\n"
        f"Was: {before} → Now: rank {updated.get('rank', '—')}, "
        f"{updated.get('status', '—')}{note}\n\n"
        f"**Resulting order for {submission_month}:**\n\n{table}"
    )


# ============================================================================
# Tool 7: get_bu_history
# ============================================================================
@mcp.tool()
def get_bu_history(bu_name: str) -> str:
    """One Business Unit's full submission history across every month --
    rank, status and timing over time, rather than a single-month snapshot.

    Args:
        bu_name: Business Unit name (case-insensitive).

    Returns:
        Markdown: a summary line (months submitted, best/average rank,
        on-time record) followed by a month-by-month table.
    """
    client = get_client()

    business_unit = find_business_unit(client, bu_name)
    if not business_unit:
        return f"No Business Unit found matching '{bu_name}'."

    resp = (
        client.table("monthly_reports")
        .select("month_year, rank, status, file_name, submitted_at")
        .eq("bu_id", business_unit["id"])
        .order("month_year", desc=True)
        .execute()
    )
    records = resp.data or []
    if not records:
        return f"'{business_unit['bu_name']}' has not submitted any reports yet."

    ranks = [r["rank"] for r in records if r.get("rank") is not None]
    late_count = sum(1 for r in records if r.get("status") == "Late")

    if ranks:
        rank_line = (
            f"**Best rank:** {min(ranks)} · "
            f"**Average rank:** {sum(ranks) / len(ranks):.1f} · "
            f"**Worst rank:** {max(ranks)}"
        )
    else:
        rank_line = "**Rank:** none recorded"

    summary = (
        f"**Months submitted:** {len(records)}  \n"
        f"{rank_line}  \n"
        f"**On time:** {len(records) - late_count} · **Late:** {late_count}\n"
    )

    rows = [
        {
            "Month": r.get("month_year", "—"),
            "Rank": r["rank"] if r.get("rank") is not None else "—",
            "Status": r.get("status", "—"),
            "Vs Deadline": describe_margin(r.get("submitted_at"), r.get("month_year", "")),
            "File": r.get("file_name", "—"),
            "Submitted At (MMT)": format_mmt(r.get("submitted_at")),
        }
        for r in records
    ]

    columns = ["Month", "Rank", "Status", "Vs Deadline", "File", "Submitted At (MMT)"]
    return (
        f"### {business_unit['bu_name']} — Submission History\n\n"
        f"{summary}\n{rows_to_markdown(rows, columns)}"
    )


# ============================================================================
# Tool 8: validate_submission
# ============================================================================
@mcp.tool()
def validate_submission(
    bu_name: str,
    submission_month: str,
    required_sheets: Optional[str] = None,
    required_columns: Optional[str] = None,
) -> str:
    """Check that a submitted file is actually usable, rather than just
    present.

    The app accepts any uploaded file unparsed, so a corrupt, empty, or
    wrong-shaped workbook counts as "Submitted" until someone opens it. This
    downloads the file and reports concrete problems: won't parse, no
    sheets, zero data rows, entirely-blank columns, unnamed columns, and any
    required sheets/columns that are absent.

    Args:
        bu_name: Business Unit name (case-insensitive).
        submission_month: 'YYYY-MM' period.
        required_sheets: Optional comma-separated sheet names that must
            exist (case-insensitive), e.g. 'Summary,Details'.
        required_columns: Optional comma-separated column names that must
            appear in at least one sheet (case-insensitive).

    Returns:
        Markdown: PASS/FAIL verdict, then per-sheet findings and a list of
        issues.
    """
    client = get_client()

    try:
        parse_month(submission_month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    business_unit = find_business_unit(client, bu_name)
    if not business_unit:
        return f"No Business Unit found matching '{bu_name}'."

    report = find_report(client, business_unit["id"], submission_month)
    if not report:
        return f"No submission found for '{business_unit['bu_name']}' in {submission_month}."

    title = f"### Validation — {business_unit['bu_name']} · {submission_month}\n\n"
    file_name = report.get("file_name") or ""
    ext = os.path.splitext(file_name)[1].lower()
    issues: list = []

    try:
        content = download_file_bytes(client, report.get("file_path", ""), report.get("file_url", ""))
    except Exception as exc:
        return title + f"**FAIL** — the record exists but the file could not be downloaded: {exc}"

    if not content:
        return title + "**FAIL** — the stored file is empty (0 bytes)."

    tables: dict = {}
    if ext in EXCEL_EXTENSIONS:
        try:
            engine = "openpyxl" if ext == ".xlsx" else "xlrd"
            tables = pd.read_excel(io.BytesIO(content), sheet_name=None, engine=engine)
        except Exception as exc:
            return title + f"**FAIL** — file will not parse as Excel: {exc}"
    elif ext in CSV_EXTENSIONS:
        try:
            tables = {"(csv)": pd.read_csv(io.BytesIO(content))}
        except Exception as exc:
            return title + f"**FAIL** — file will not parse as CSV: {exc}"
    else:
        return title + (
            f"**INCONCLUSIVE** — `{ext or 'no extension'}` is not a spreadsheet format this tool "
            f"can inspect (only .xlsx, .xls, .csv). The file exists ({len(content):,} bytes) but "
            f"its contents cannot be checked automatically."
        )

    if not tables:
        return title + "**FAIL** — the workbook contains no sheets."

    findings = []
    all_columns_lower = set()
    for sheet_name, df in tables.items():
        columns = [str(c) for c in df.columns]
        all_columns_lower.update(c.strip().lower() for c in columns)

        blank_columns = [c for c in columns if df[c].isna().all()]
        unnamed_columns = [c for c in columns if c.strip() == "" or c.lower().startswith("unnamed:")]

        if len(df) == 0:
            issues.append(f"Sheet `{sheet_name}` has headers but zero data rows.")
        if blank_columns:
            issues.append(f"Sheet `{sheet_name}` has entirely blank column(s): {', '.join(blank_columns[:8])}")
        if unnamed_columns:
            issues.append(
                f"Sheet `{sheet_name}` has {len(unnamed_columns)} unnamed/placeholder column(s) "
                f"-- often a sign the header row is not the first row."
            )

        findings.append(
            {
                "Sheet": sheet_name,
                "Rows": len(df),
                "Columns": len(columns),
                "Blank Cols": len(blank_columns),
                "Unnamed Cols": len(unnamed_columns),
            }
        )

    wanted_sheets = split_csv_arg(required_sheets)
    if wanted_sheets:
        present = {str(s).strip().lower() for s in tables}
        for sheet in wanted_sheets:
            if sheet.lower() not in present:
                issues.append(f"Required sheet `{sheet}` is missing.")

    wanted_columns = split_csv_arg(required_columns)
    if wanted_columns:
        for column in wanted_columns:
            if column.lower() not in all_columns_lower:
                issues.append(f"Required column `{column}` appears in no sheet.")

    verdict = "**PASS** — no problems found." if not issues else f"**FAIL** — {len(issues)} issue(s) found."
    body = (
        f"{verdict}\n\n"
        f"**File:** {file_name} ({len(content):,} bytes) · **Status:** {report.get('status', '—')}\n\n"
        f"{rows_to_markdown(findings, ['Sheet', 'Rows', 'Columns', 'Blank Cols', 'Unnamed Cols'])}\n"
    )
    if issues:
        body += "\n**Issues:**\n\n" + "\n".join(f"- {issue}" for issue in issues)
    return title + body


# ============================================================================
# Tool 9: get_ranking_criteria
# ============================================================================
@mcp.tool()
def get_ranking_criteria() -> str:
    """Explain exactly how rank and status are decided in this system, so a
    given rank can be justified to the BU that received it.

    Takes no arguments -- this describes the rules as implemented in code
    (ranking.py / db.py), not data.

    Returns:
        Markdown description of the ranking rule, the deadline, the status
        values, and the known limitations of the current scheme.
    """
    return f"""### How ranking works

**Rank = arrival order within the month.**
The first Business Unit to submit for a period gets rank 1, the second rank
2, and so on. It is assigned at submission time by counting the reports
already recorded for that month (db.get_next_rank / next_rank here). It is
**not** a quality or performance score -- no metric or scoring data is
collected in this version; a BU simply uploads a report file, unparsed.

**Deadline: the {DEADLINE_DAY}th of the reporting month, 23:59:59 MMT.**
A fixed code constant (ranking.DEADLINE_DAY), identical in the app and in
this server. Myanmar Standard Time (UTC+06:30, no DST) is authoritative --
the app server usually runs on UTC, and using its clock would misfile
submissions made near the day boundary.

**Status values** (enforced by a CHECK constraint on monthly_reports):

| Status | Meaning |
| --- | --- |
| Submitted | Uploaded on or before the deadline |
| Late | Uploaded after the deadline |
| Pending | No submission recorded for that BU/month yet |

**Other rules**

- One report per BU per month (unique constraint on bu_id + month_year).
  Resubmitting requires an explicit overwrite, which keeps the original rank.
- An admin may override rank or status afterward (set_report_rank_status).
- Deleting a report closes the rank gap it leaves, so a month stays a
  contiguous 1..N.

**Known limitations of ranking by arrival order**

- It rewards speed only. A thorough report filed on the 10th ranks below a
  one-line file filed on the 2nd.
- It is not comparable across months: rank 3 of 4 BUs and rank 3 of 20 are
  very different results.
- Nothing checks that the uploaded file is usable -- run validate_submission
  for that.

Introducing a quality score would mean adding score columns to
monthly_reports and a rule for combining them with timeliness. The current
scheme is deliberately simple; it just needs to be described as
"submission order", not "performance ranking", when it is shown to BUs."""


# ============================================================================
# Tool 10: compare_bus
# ============================================================================
@mcp.tool()
def compare_bus(submission_month: str) -> str:
    """Full side-by-side view of every Business Unit for one month --
    submitted and missing together, with timing relative to the deadline.

    This is the roster left-joined onto the month's reports (the same view
    the app's ranking table shows), which get_submission_rankings cannot
    produce because it only reads actual submissions.

    Args:
        submission_month: 'YYYY-MM' period to compare (e.g. '2026-08').

    Returns:
        Markdown: headline counts, then one row per BU -- rank, status,
        margin vs the deadline, file, and submission time.
    """
    client = get_client()

    try:
        deadline = get_deadline(submission_month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    business_units = list_all_business_units(client)
    if not business_units:
        return "_No Business Units are registered yet._"

    reports = {r["bu_id"]: r for r in reports_for_month(client, submission_month)}

    rows = []
    for bu in business_units:
        report = reports.get(bu["id"])
        if report:
            rows.append(
                {
                    "Rank": report["rank"] if report.get("rank") is not None else "—",
                    "Business Unit": bu.get("bu_name", "—"),
                    "Status": report.get("status", "—"),
                    "Vs Deadline": describe_margin(report.get("submitted_at"), submission_month),
                    "File": report.get("file_name", "—"),
                    "Submitted At (MMT)": format_mmt(report.get("submitted_at")),
                    "_sort": report["rank"] if report.get("rank") is not None else 10**6,
                }
            )
        else:
            rows.append(
                {
                    "Rank": "—",
                    "Business Unit": bu.get("bu_name", "—"),
                    "Status": "Pending",
                    "Vs Deadline": "—",
                    "File": "—",
                    "Submitted At (MMT)": "—",
                    "_sort": 10**7,
                }
            )

    rows.sort(key=lambda r: (r["_sort"], r["Business Unit"]))

    submitted = sum(1 for r in rows if r["Status"] in ("Submitted", "Late"))
    late = sum(1 for r in rows if r["Status"] == "Late")
    pending = len(rows) - submitted

    columns = ["Rank", "Business Unit", "Status", "Vs Deadline", "File", "Submitted At (MMT)"]
    return (
        f"### BU Comparison — {submission_month}\n\n"
        f"**{submitted}/{len(rows)} submitted** · {late} late · {pending} pending  \n"
        f"**Deadline:** {deadline.strftime('%Y-%m-%d %H:%M')} MMT\n\n"
        f"{rows_to_markdown(rows, columns)}"
    )


# ============================================================================
# Tool 11: get_deadline_status
# ============================================================================
@mcp.tool()
def get_deadline_status(submission_month: Optional[str] = None) -> str:
    """Where a month stands against its deadline right now: time remaining
    or overdue, and the on-time / late / missing split.

    Args:
        submission_month: 'YYYY-MM' period. Defaults to the CURRENT month in
            Myanmar time.

    Returns:
        Markdown: the deadline, time to or past it, and counts plus the
        names in each bucket.
    """
    client = get_client()

    now = now_mmt()
    month = submission_month or now.strftime("%Y-%m")

    try:
        deadline = get_deadline(month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    delta = deadline - now
    total_hours = abs(delta.total_seconds()) / 3600
    magnitude = f"{total_hours / 24:.1f} day(s)" if total_hours >= 24 else f"{total_hours:.1f} hour(s)"
    countdown = f"**{magnitude} remaining**" if delta.total_seconds() > 0 else f"**overdue by {magnitude}**"

    business_units = list_all_business_units(client)
    reports = {r["bu_id"]: r for r in reports_for_month(client, month)}

    on_time, late, missing = [], [], []
    for bu in business_units:
        report = reports.get(bu["id"])
        if not report:
            missing.append(bu.get("bu_name", "—"))
        elif report.get("status") == "Late":
            late.append(bu.get("bu_name", "—"))
        else:
            on_time.append(bu.get("bu_name", "—"))

    def bucket(label: str, names: list) -> str:
        if not names:
            return f"**{label}:** 0\n"
        return f"**{label}:** {len(names)} — {', '.join(sorted(names))}\n"

    return (
        f"### Deadline Status — {month}\n\n"
        f"**Deadline:** {deadline.strftime('%Y-%m-%d %H:%M')} MMT (the {DEADLINE_DAY}th)  \n"
        f"**Now:** {now.strftime('%Y-%m-%d %H:%M')} MMT — {countdown}\n\n"
        f"{bucket('On time', on_time)}"
        f"{bucket('Late', late)}"
        f"{bucket('Not submitted', missing)}"
    )


# ============================================================================
# Tool 12: draft_reminder_message
# ============================================================================
@mcp.tool()
def draft_reminder_message(
    submission_month: Optional[str] = None,
    language: str = "both",
) -> str:
    """Draft a ready-to-send reminder for the Business Units that still have
    not submitted, in English, Burmese, or both.

    This composes the text only -- it does not send anything. Pair it with an
    email/chat tool to deliver it, or with a scheduled task to run a few days
    before each deadline.

    Args:
        submission_month: 'YYYY-MM' period. Defaults to the CURRENT month in
            Myanmar time.
        language: 'en', 'my', or 'both' (default).

    Returns:
        The recipient list and the drafted message text.
    """
    client = get_client()

    now = now_mmt()
    month = submission_month or now.strftime("%Y-%m")

    try:
        deadline = get_deadline(month)
    except ValueError as exc:
        return f"**Invalid input:** {exc}"

    language = (language or "both").strip().lower()
    if language not in ("en", "my", "both"):
        return "**Invalid language** -- use 'en', 'my', or 'both'."

    business_units = list_all_business_units(client)
    submitted_ids = {r["bu_id"] for r in reports_for_month(client, month)}
    missing = sorted(bu["bu_name"] for bu in business_units if bu["id"] not in submitted_ids)

    if not missing:
        return (
            f"### No reminder needed — {month}\n\n"
            f"All {len(business_units)} Business Unit(s) have already submitted."
        )

    overdue = now > deadline
    deadline_text = deadline.strftime("%d %B %Y, %H:%M")
    days = abs((deadline - now).days)

    english = (
        f"Subject: {'OVERDUE' if overdue else 'Reminder'} — {month} monthly report\n\n"
        f"Hello,\n\n"
        f"Our records show your {month} monthly report has not been received.\n\n"
        + (
            f"The deadline was {deadline_text} MMT and has now passed by {days} day(s). "
            f"Please upload your report as soon as possible; it will be recorded as Late.\n\n"
            if overdue
            else f"The deadline is {deadline_text} MMT — {days} day(s) from now. "
            f"Reports uploaded after that time are recorded as Late.\n\n"
        )
        + "Please upload your file through the reporting portal.\n\n"
        "Thank you."
    )

    burmese = (
        f"အကြောင်းအရာ: {month} လစဉ်အစီရင်ခံစာ "
        f"{'ရက်ကျော်လွန်နေပါပြီ' if overdue else 'သတိပေးချက်'}\n\n"
        f"မင်္ဂလာပါ၊\n\n"
        f"{month} အတွက် လစဉ်အစီရင်ခံစာ လက်ခံရရှိခြင်း မရှိသေးကြောင်း တွေ့ရပါသည်။\n\n"
        + (
            f"နောက်ဆုံးထားတင်သွင်းရမည့်ရက်မှာ {deadline_text} (မြန်မာစံတော်ချိန်) ဖြစ်ပြီး "
            f"ယခုအခါ {days} ရက် ကျော်လွန်သွားပါပြီ။ ဖြစ်နိုင်သမျှ အမြန်ဆုံး တင်သွင်းပေးပါရန် "
            f"မေတ္တာရပ်ခံအပ်ပါသည်။ ယခုတင်သွင်းပါက \"Late\" အဖြစ် မှတ်တမ်းတင်မည်ဖြစ်ပါသည်။\n\n"
            if overdue
            else f"နောက်ဆုံးထားတင်သွင်းရမည့်ရက်မှာ {deadline_text} (မြန်မာစံတော်ချိန်) ဖြစ်ပြီး "
            f"{days} ရက် ကျန်ရှိပါသေးသည်။ သတ်မှတ်ချိန်ကျော်လွန်ပါက \"Late\" အဖြစ် "
            f"မှတ်တမ်းတင်မည်ဖြစ်ပါသည်။\n\n"
        )
        + "ကျေးဇူးပြု၍ reporting portal မှတစ်ဆင့် ဖိုင်တင်သွင်းပေးပါ။\n\n"
        "ကျေးဇူးတင်ပါသည်။"
    )

    sections = [
        f"### Reminder draft — {month}\n",
        f"**Send to ({len(missing)}):** {', '.join(missing)}  \n",
        f"**Deadline:** {deadline_text} MMT — "
        f"{'passed ' + str(days) + ' day(s) ago' if overdue else str(days) + ' day(s) remaining'}\n",
    ]
    if language in ("en", "both"):
        sections.append(f"\n---\n\n**English**\n\n```\n{english}\n```\n")
    if language in ("my", "both"):
        sections.append(f"\n---\n\n**မြန်မာ**\n\n```\n{burmese}\n```\n")

    return "".join(sections)


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
#      "Which BUs haven't submitted for 2026-08?"
#      "Compare all BUs for 2026-08"
#      "How close is the 2026-08 deadline?"
#      "Draft a reminder for the BUs still missing this month"
#      "Show me Sales' full submission history"
#      "Validate the Sales 2026-08 file -- it must have a Summary sheet"
#      "Submit C:\\reports\\sales_aug.xlsx as the Sales report for 2026-08"
#      "Move Sales to rank 1 for 2026-08"
#
# 5. NOTE ON THE WRITE TOOLS (submit_bu_report, set_report_rank_status):
#    they run with the service-role key, so RLS does not apply and no
#    is_admin() check is enforced -- anyone who can talk to this server
#    can write as an admin. It is intended as a local operator tool, not
#    something to expose over a network. Keep DEADLINE_DAY here in sync
#    with ranking.DEADLINE_DAY in the app; both are code constants.
