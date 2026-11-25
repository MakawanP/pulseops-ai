"""SQLite schema, connection management, read-only SQL safety guard, and enterprise seed data."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "pulseops.db"

DEPARTMENTS = ("Engineering", "Marketing", "Sales", "Operations", "G&A")
MONTHS = tuple(f"2025-{m:02d}" for m in range(1, 13))

ALLOWED_APP_TABLES = frozenset(
    {
        "budgets",
        "transactions",
        "subscriptions",
        "etl_runs",
        "workflow_runs",
        "eval_runs",
    }
)

FORBIDDEN_SQL_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "PRAGMA",
        "ATTACH",
        "DETACH",
        "VACUUM",
        "REINDEX",
        "REPLACE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "TRIGGER",
        "EXEC",
        "EXECUTE",
        "RECURSIVE",
        "LOAD_EXTENSION",
    }
)

BLOCKED_SQL_FUNCTIONS = frozenset(
    {
        "ZEROBLOB",
        "RANDOMBLOB",
        "LOAD_EXTENSION",
    }
)

ALLOWED_SQL_FUNCTIONS = frozenset(
    {
        # Aggregates
        "count",
        "sum",
        "total",
        "avg",
        "min",
        "max",
        "group_concat",
        # Math / Numeric
        "abs",
        "round",
        "sign",
        "ceil",
        "ceiling",
        "floor",
        "trunc",
        "mod",
        "pow",
        "power",
        "sqrt",
        "exp",
        "ln",
        "log",
        "log10",
        "log2",
        # String (bounded by SQLITE_LIMIT_LENGTH)
        "lower",
        "upper",
        "length",
        "octet_length",
        "substr",
        "substring",
        "trim",
        "ltrim",
        "rtrim",
        "instr",
        "unicode",
        "like",
        "glob",
        "concat",
        "concat_ws",
        "hex",
        # Null / Conditional / Type
        "coalesce",
        "ifnull",
        "nullif",
        "iif",
        "typeof",
        "unlikely",
        "likely",
        "likelihood",
        # Date & Time
        "date",
        "time",
        "datetime",
        "julianday",
        "unixepoch",
        "strftime",
        "timediff",
        # Window functions
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        # Safe read-only JSON inspection
        "json_extract",
        "json_type",
        "json_array_length",
        "json_valid",
    }
)

SQL_MAX_EXECUTION_SECONDS = 2.0
SQL_PROGRESS_STEP_INTERVAL = 10_000
SQL_MAX_PROGRESS_TICKS = 500  # 5,000,000 SQLite VM opcodes
SQL_MAX_LENGTH_BYTES = 1_000_000  # 1 MB max string/blob allocation

_LOCK_GUARD = threading.Lock()
_DB_LOCKS: dict[Path, threading.RLock] = {}

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        department TEXT NOT NULL,
        budget_amount REAL NOT NULL,
        UNIQUE(month, department)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_date TEXT NOT NULL,
        month TEXT NOT NULL,
        department TEXT NOT NULL,
        vendor TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        invoice_id TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'erp_seed',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_transactions_month ON transactions(month)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_dept ON transactions(department)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_invoice ON transactions(invoice_id)",
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor TEXT NOT NULL,
        department TEXT NOT NULL,
        category TEXT NOT NULL,
        seats INTEGER NOT NULL,
        monthly_cost REAL NOT NULL,
        prev_monthly_cost REAL NOT NULL,
        last_used_days INTEGER NOT NULL,
        renewal_date TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS etl_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at TEXT NOT NULL,
        filename TEXT NOT NULL,
        status TEXT NOT NULL,
        rows_total INTEGER NOT NULL,
        rows_inserted INTEGER NOT NULL,
        duplicates_skipped INTEGER NOT NULL,
        anomalies_flagged INTEGER NOT NULL,
        duration_ms REAL NOT NULL,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id TEXT NOT NULL,
        workflow_name TEXT NOT NULL,
        ran_at TEXT NOT NULL,
        status TEXT NOT NULL,
        duration_ms REAL NOT NULL,
        params_json TEXT NOT NULL,
        summary TEXT NOT NULL,
        steps_json TEXT NOT NULL,
        output_markdown TEXT NOT NULL,
        webhook_payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ran_at TEXT NOT NULL,
        total INTEGER NOT NULL,
        passed INTEGER NOT NULL,
        pass_rate REAL NOT NULL,
        avg_latency_ms REAL NOT NULL,
        categories_json TEXT NOT NULL,
        cases_json TEXT NOT NULL
    )
    """,
)

SCHEMA_SQL = ";\n".join(stmt.strip() for stmt in SCHEMA_STATEMENTS) + ";\n"


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve database path from explicit argument, PULSEOPS_DB_PATH env var, or default."""
    if db_path is not None and str(db_path).strip():
        return Path(db_path).expanduser().resolve()
    env_path = os.environ.get("PULSEOPS_DB_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return DEFAULT_DB_PATH.resolve()


def get_db_lock(db_path: str | Path | None = None) -> threading.RLock:
    """Return a per-database re-entrant lock used to serialize writes and atomic resets."""
    resolved = resolve_db_path(db_path)
    with _LOCK_GUARD:
        lock = _DB_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _DB_LOCKS[resolved] = lock
        return lock


def get_connection(db_path: str | Path | None = None, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection configured with Row factory, busy_timeout, and optional read-only mode."""
    resolved = resolve_db_path(db_path)
    if read_only:
        uri = f"{resolved.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA query_only = ON")
        return conn

    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_readonly_snapshot(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a read-only SQLite connection wrapped in a single consistent WAL read transaction."""
    conn = get_connection(db_path, read_only=True)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()


def _strip_sql_comments_and_strings(sql: str) -> tuple[str, bool]:
    """Strip comments and replace string literals with placeholders, detecting multi-statement ';'.

    Returns (sanitized_sql_for_token_inspection, has_internal_semicolon).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_single:
            if ch == "'" and nxt == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
                out.append(" ")
            i += 1
            continue

        if in_double:
            if ch == '"' and nxt == '"':
                i += 2
                continue
            if ch == '"':
                in_double = False
                out.append(" ")
            i += 1
            continue

        # Line comment -- ...
        if ch == "-" and nxt == "-":
            i += 2
            while i < n and sql[i] not in ("\n", "\r"):
                i += 1
            out.append(" ")
            continue

        # Block comment /* ... */
        if ch == "/" and nxt == "*":
            i += 2
            closed = False
            while i + 1 < n:
                if sql[i] == "*" and sql[i + 1] == "/":
                    i += 2
                    closed = True
                    break
                i += 1
            if not closed:
                raise ValueError("Unterminated block comment in SQL query.")
            out.append(" ")
            continue

        if ch == "'":
            in_single = True
            out.append(" '' ")
            i += 1
            continue

        if ch == '"':
            in_double = True
            out.append(" ")
            i += 1
            continue

        out.append(ch)
        i += 1

    if in_single or in_double:
        raise ValueError("Unterminated string or identifier literal in SQL query.")

    sanitized = "".join(out)
    semi_count = sanitized.count(";")
    has_internal_semicolon = False
    if semi_count > 1:
        has_internal_semicolon = True
    elif semi_count == 1:
        _, after = sanitized.split(";", 1)
        if after.strip():
            has_internal_semicolon = True

    return sanitized, has_internal_semicolon


def validate_readonly_sql(sql: str) -> str:
    """Strictly validate that SQL is a single read-only SELECT or WITH...SELECT statement.

    Raises ValueError on any mutating keyword, PRAGMA/pragma_*, RECURSIVE, ATTACH,
    blocked memory-bomb functions, internal sqlite_* objects, or multi-statement ';'.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL query cannot be empty.")

    cleaned = sql.strip()
    sanitized, has_internal_semicolon = _strip_sql_comments_and_strings(cleaned)

    if has_internal_semicolon:
        raise ValueError("Read-only guard rejected multi-statement SQL containing ';'.")

    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sanitized)
    if not tokens:
        raise ValueError("SQL query contains no valid statement.")

    upper_tokens = [tok.upper() for tok in tokens]
    first = upper_tokens[0]
    if first not in ("SELECT", "WITH"):
        raise ValueError(
            f"Read-only guard allows only SELECT or WITH...SELECT queries (got '{first}')."
        )

    for tok in upper_tokens:
        if tok in FORBIDDEN_SQL_KEYWORDS:
            raise ValueError(f"Read-only guard blocked forbidden SQL keyword: {tok}.")
        if tok.startswith("PRAGMA_"):
            raise ValueError(f"Read-only guard blocked forbidden PRAGMA table function: {tok}.")
        if tok.startswith("SQLITE_"):
            raise ValueError(f"Read-only guard blocked internal SQLite object: {tok}.")
        if tok in BLOCKED_SQL_FUNCTIONS:
            raise ValueError(f"Read-only guard blocked restricted SQL function: {tok}.")

    if first == "WITH" and "SELECT" not in upper_tokens:
        raise ValueError("WITH common table expression must terminate in a SELECT query.")

    return cleaned.rstrip(";").strip()


def _readonly_sql_authorizer(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    arg3: str | None,
    arg4: str | None,
) -> int:
    """SQLite authorizer restricting user SQL strictly to SELECT + reads on app tables + safe functions."""
    if action_code == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK

    if action_code == sqlite3.SQLITE_READ:
        table_name = (arg1 or "").lower()
        db_name = (arg3 or "main").lower()
        if table_name in ALLOWED_APP_TABLES and db_name in ("main", ""):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    if action_code == sqlite3.SQLITE_FUNCTION:
        func_name = (arg2 or "").lower()
        if func_name in ALLOWED_SQL_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_DENY


def _sanitize_sql_cell(value: Any) -> Any:
    """Convert SQLite values into strict RFC-8259 JSON-safe primitives."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def execute_readonly_sql(
    db_path: str | Path | None, sql: str, max_rows: int = 500
) -> dict[str, Any]:
    """Validate and execute a sandboxed read-only SQL query against SQLite."""
    validated_sql = validate_readonly_sql(sql)
    started = time.perf_counter()

    conn = get_connection(db_path, read_only=True)
    aborted_reason: list[str] = []
    try:
        if hasattr(conn, "setlimit") and hasattr(sqlite3, "SQLITE_LIMIT_LENGTH"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_LENGTH_BYTES)

        deadline = started + SQL_MAX_EXECUTION_SECONDS
        ticks = 0

        def _progress_handler() -> int:
            nonlocal ticks
            ticks += 1
            if ticks > SQL_MAX_PROGRESS_TICKS:
                aborted_reason.append("opcode complexity limit")
                return 1
            if time.perf_counter() > deadline:
                aborted_reason.append(f"{SQL_MAX_EXECUTION_SECONDS:.1f}s timeout")
                return 1
            return 0

        conn.set_progress_handler(_progress_handler, SQL_PROGRESS_STEP_INTERVAL)
        conn.set_authorizer(_readonly_sql_authorizer)

        cursor = conn.execute(validated_sql)
        columns = [desc[0] for desc in (cursor.description or [])]
        fetched = cursor.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        if truncated:
            fetched = fetched[:max_rows]
        rows = [[_sanitize_sql_cell(row[i]) for i in range(len(columns))] for row in fetched]
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "ok": True,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "duration_ms": duration_ms,
        }
    except sqlite3.Error as exc:
        if aborted_reason:
            raise ValueError(
                f"SQL query aborted after exceeding {aborted_reason[0]}."
            ) from exc
        raise ValueError(f"SQL execution error: {exc}") from exc
    finally:
        try:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
        except sqlite3.Error:
            pass
        conn.close()


def _build_seed_budgets() -> list[tuple[str, str, float]]:
    """Build 12 months x 5 departments = 60 realistic FY2025 monthly budget rows ($1,740,000 total)."""
    h1_monthly = {
        "Engineering": 46000.0,
        "Marketing": 28000.0,
        "Sales": 25000.0,
        "Operations": 21000.0,
        "G&A": 18000.0,
    }
    h2_monthly = {
        "Engineering": 50000.0,
        "Marketing": 32000.0,
        "Sales": 27000.0,
        "Operations": 23000.0,
        "G&A": 20000.0,
    }
    rows: list[tuple[str, str, float]] = []
    for idx, month in enumerate(MONTHS):
        table = h1_monthly if idx < 6 else h2_monthly
        for dept in DEPARTMENTS:
            rows.append((month, dept, table[dept]))
    return rows


def _build_seed_transactions() -> list[tuple[str, str, str, str, str, float, str, str, str, str]]:
    """Build 106 deterministic FY2025 enterprise ledger rows with intentional spikes and duplicates.

    Department profile vs FY2025 Budget ($1,740,000):
    - Engineering ($576,000 budget): over_budget (+11.34%) driven by Q3/Q4 GPU/Databricks spikes + duplicate AWS bill
    - Marketing   ($360,000 budget): over_budget (+7.58%) driven by Q4 Google Ads spike + duplicate media invoice
    - Sales       ($312,000 budget): watch (+2.63%) driven by Q4 field travel spike
    - Operations  ($264,000 budget): on_track (-3.71%) disciplined spend
    - G&A         ($228,000 budget): on_track (-5.26%) disciplined spend
    """
    seeded_at = "2025-12-01T08:00:00Z"
    txs: list[tuple[str, str, str, str, str, float, str, str, str, str]] = []

    def add(
        tx_date: str,
        dept: str,
        vendor: str,
        category: str,
        amount: float,
        invoice_id: str,
        desc: str,
    ) -> None:
        txs.append(
            (tx_date, tx_date[:7], dept, vendor, category, round(amount, 2), invoice_id, desc, "erp_seed", seeded_at)
        )

    # --- ENGINEERING (24 baseline + 1 duplicate row + 2 spend spikes = 27 rows) ---
    eng_aws_amounts = [
        19200.0, 19800.0, 20100.0, 19500.0, 20600.0, 21100.0,
        18450.0, 21400.0, 21900.0, 22300.0, 22800.0, 21600.0,
    ]
    eng_secondary = [
        ("Databricks", "Data Platform", 20400.0, "Production lakehouse compute"),
        ("Snowflake", "Data Warehouse", 18900.0, "Analytics warehouse credits"),
        ("Datadog", "Observability", 19600.0, "APM logs and infrastructure monitoring"),
        ("OpenAI API", "AI Infrastructure", 20800.0, "Production LLM inference tokens"),
        ("Databricks", "Data Platform", 21200.0, "Feature store and ETL clusters"),
        ("GitHub Enterprise", "DevTools", 19400.0, "Enterprise engineering licenses"),
        ("Snowflake", "Data Warehouse", 21500.0, "Enterprise BI query compute"),
        ("OpenAI API", "AI Infrastructure", 22400.0, "Copilot and eval inference usage"),
        ("Datadog", "Observability", 21800.0, "Distributed tracing and metrics"),
        ("Databricks", "Data Platform", 22900.0, "ML model training pipelines"),
        ("Snowflake", "Data Warehouse", 22100.0, "Q4 analytics warehouse expansion"),
        ("OpenAI API", "AI Infrastructure", 21700.0, "Production agent token consumption"),
    ]
    for idx, month in enumerate(MONTHS, start=1):
        aws_inv = "INV-2025-0742" if idx == 7 else f"INV-2025-{idx:02d}01"
        add(
            f"{month}-14",
            "Engineering",
            "AWS Cloud Services",
            "Cloud Infrastructure",
            eng_aws_amounts[idx - 1],
            aws_inv,
            f"Monthly production cloud compute ({month})",
        )
        sec_vendor, sec_cat, sec_amt, sec_desc = eng_secondary[idx - 1]
        add(
            f"{month}-20",
            "Engineering",
            sec_vendor,
            sec_cat,
            sec_amt,
            f"INV-2025-{idx:02d}02",
            sec_desc,
        )

    # Intentional Engineering duplicate invoice (ERP double-posting of July AWS invoice)
    add(
        "2025-07-16",
        "Engineering",
        "AWS Cloud Services",
        "Cloud Infrastructure",
        18450.0,
        "INV-2025-0742",
        "Duplicate ERP posting of July cloud compute invoice",
    )
    # Intentional Engineering high-severity spend spikes
    add(
        "2025-09-22",
        "Engineering",
        "AWS Cloud Services",
        "Cloud Infrastructure",
        62500.0,
        "INV-2025-0915",
        "Unscheduled H100 GPU cluster training burst",
    )
    add(
        "2025-11-18",
        "Engineering",
        "Databricks",
        "Data Platform",
        58900.0,
        "INV-2025-1108",
        "Runaway auto-scaling ML pipeline cluster job",
    )

    # --- MARKETING (22 baseline + 1 duplicate row + 1 spend spike = 24 rows) ---
    mkt_google_amounts = [
        14200.0, 14600.0, 15100.0, 14800.0, 15400.0, 15200.0,
        15800.0, 16100.0, 16400.0, 16200.0, 16800.0, 16500.0,
    ]
    for idx, month in enumerate(MONTHS, start=1):
        add(
            f"{month}-10",
            "Marketing",
            "Google Ads",
            "Paid Acquisition",
            mkt_google_amounts[idx - 1],
            f"INV-2025-{idx:02d}11",
            f"Search and performance acquisition ({month})",
        )

    mkt_secondary = [
        ("2025-01-19", "LinkedIn Marketing", "Demand Gen", 13600.0, "INV-2025-0112", "Q1 enterprise ABM campaigns"),
        ("2025-02-18", "HubSpot", "MarTech", 13200.0, "INV-2025-0212", "Marketing automation suite billing"),
        ("2025-03-21", "SignalBoost Media", "Agency & Events", 14100.0, "INV-2025-0312", "Q1 demand gen creative retainer"),
        ("2025-04-17", "LinkedIn Marketing", "Demand Gen", 14400.0, "INV-2025-0412", "Mid-market sponsored content"),
        ("2025-05-19", "HubSpot", "MarTech", 13500.0, "INV-2025-0512", "CRM sync and lifecycle workflows"),
        ("2025-06-22", "SignalBoost Media", "Agency & Events", 14800.0, "INV-2025-0612", "Mid-year FinTech conference booth"),
        ("2025-07-18", "LinkedIn Marketing", "Demand Gen", 14900.0, "INV-2025-0712", "Q3 pipeline acceleration ads"),
        ("2025-08-21", "HubSpot", "MarTech", 13900.0, "INV-2025-0812", "Enterprise attribution analytics"),
        ("2025-10-08", "SignalBoost Media", "Agency & Events", 14200.0, "INV-2025-1019", "Q4 brand awareness agency retainer"),
        ("2025-12-12", "LinkedIn Marketing", "Demand Gen", 14600.0, "INV-2025-1212", "Year-end CFO persona campaign"),
    ]
    for tx_date, vendor, cat, amt, inv, desc in mkt_secondary:
        add(tx_date, "Marketing", vendor, cat, amt, inv, desc)

    # Intentional Marketing duplicate invoice (double-billed agency invoice)
    add(
        "2025-10-10",
        "Marketing",
        "SignalBoost Media",
        "Agency & Events",
        14200.0,
        "INV-2025-1019",
        "Duplicate vendor resubmission of October agency retainer",
    )
    # Intentional Marketing high-severity spend spike
    add(
        "2025-11-25",
        "Marketing",
        "Google Ads",
        "Paid Acquisition",
        44800.0,
        "INV-2025-1124",
        "Uncapped Q4 holiday paid search bidding spike",
    )

    # --- SALES (20 baseline + 1 medium-severity spend spike = 21 rows) ---
    sales_core_amounts = [
        14800.0, 15200.0, 15600.0, 14400.0, 15800.0, 15100.0,
        15900.0, 16200.0, 15400.0, 16500.0, 16100.0, 15700.0,
    ]
    for idx, month in enumerate(MONTHS, start=1):
        add(
            f"{month}-08",
            "Sales",
            "Salesforce",
            "CRM",
            sales_core_amounts[idx - 1],
            f"INV-2025-{idx:02d}21",
            f"Enterprise CRM & Sales Cloud seats ({month})",
        )

    sales_secondary = [
        ("2025-01-22", "Gong.io", "Revenue Intel", 13200.0, "INV-2025-0122", "Call recording and deal intelligence"),
        ("2025-03-19", "Delta Corporate Travel", "Field Travel", 14600.0, "INV-2025-0322", "Q1 enterprise account onsite visits"),
        ("2025-04-24", "Apollo.io", "Sales Ops", 12800.0, "INV-2025-0422", "B2B prospecting and enrichment credits"),
        ("2025-06-18", "Gong.io", "Revenue Intel", 13900.0, "INV-2025-0622", "Mid-year revenue enablement licenses"),
        ("2025-07-23", "Delta Corporate Travel", "Field Travel", 15100.0, "INV-2025-0722", "Summer regional customer roadshow"),
        ("2025-09-16", "Apollo.io", "Sales Ops", 13400.0, "INV-2025-0922", "Q3 outbound intent data tier"),
        ("2025-11-14", "Gong.io", "Revenue Intel", 14200.0, "INV-2025-1122", "Q4 forecast and pipeline analytics"),
        ("2025-12-11", "Delta Corporate Travel", "Field Travel", 14900.0, "INV-2025-1222", "Q4 enterprise renewal closing travel"),
    ]
    for tx_date, vendor, cat, amt, inv, desc in sales_secondary:
        add(tx_date, "Sales", vendor, cat, amt, inv, desc)

    # Intentional Sales medium-severity spend spike (z-score ~2.4-2.6)
    add(
        "2025-10-21",
        "Sales",
        "Delta Corporate Travel",
        "Field Travel",
        21400.0,
        "INV-2025-1033",
        "Unapproved Q4 executive summit travel surge",
    )

    # --- OPERATIONS (18 clean baseline rows -> on_track ~-3.7%) ---
    ops_core_amounts = [
        13900.0, 14200.0, 14100.0, 13800.0, 14400.0, 14000.0,
        14300.0, 14600.0, 14200.0, 14500.0, 14700.0, 14100.0,
    ]
    for idx, month in enumerate(MONTHS, start=1):
        add(
            f"{month}-06",
            "Operations",
            "NetSuite ERP" if idx % 2 == 1 else "Rippling",
            "Finance Systems" if idx % 2 == 1 else "HR & Payroll",
            ops_core_amounts[idx - 1],
            f"INV-2025-{idx:02d}31",
            f"Core operational platform billing ({month})",
        )

    ops_secondary = [
        ("2025-02-20", "WeWork Enterprise", "Facilities", 13600.0, "INV-2025-0232", "NYC regional office workspace"),
        ("2025-04-20", "FedEx Logistics", "Fulfillment", 14200.0, "INV-2025-0432", "Hardware provisioning and shipping"),
        ("2025-06-20", "WeWork Enterprise", "Facilities", 13800.0, "INV-2025-0632", "Q2 hybrid office facilities"),
        ("2025-08-20", "FedEx Logistics", "Fulfillment", 14500.0, "INV-2025-0832", "Q3 onboarding equipment logistics"),
        ("2025-10-20", "WeWork Enterprise", "Facilities", 14100.0, "INV-2025-1032", "Q4 office expansion lease"),
        ("2025-12-15", "FedEx Logistics", "Fulfillment", 13200.0, "INV-2025-1232", "Year-end customer hardware kits"),
    ]
    for tx_date, vendor, cat, amt, inv, desc in ops_secondary:
        add(tx_date, "Operations", vendor, cat, amt, inv, desc)

    # --- G&A (16 clean baseline rows -> on_track ~-5.3%) ---
    ga_core_amounts = [
        13200.0, 13400.0, 13600.0, 13100.0, 13500.0, 13300.0,
        13700.0, 13900.0, 13500.0, 13800.0, 13600.0, 13400.0,
    ]
    for idx, month in enumerate(MONTHS, start=1):
        vendor = "Okta" if idx % 2 == 1 else "Slack Enterprise"
        cat = "Security & IAM" if idx % 2 == 1 else "Collaboration"
        add(
            f"{month}-05",
            "G&A",
            vendor,
            cat,
            ga_core_amounts[idx - 1],
            f"INV-2025-{idx:02d}41",
            f"Enterprise {cat.lower()} platform ({month})",
        )

    ga_secondary = [
        ("2025-03-25", "Cooley LLP", "Legal & Compliance", 13800.0, "INV-2025-0342", "Q1 corporate governance & IP counsel"),
        ("2025-06-25", "Deloitte Advisory", "Audit & Tax", 13400.0, "INV-2025-0642", "Mid-year SOC2 & tax advisory"),
        ("2025-09-25", "Cooley LLP", "Legal & Compliance", 13600.0, "INV-2025-0942", "Enterprise customer MSA review"),
        ("2025-12-10", "Deloitte Advisory", "Audit & Tax", 13200.0, "INV-2025-1242", "FY2025 external audit preparation"),
    ]
    for tx_date, vendor, cat, amt, inv, desc in ga_secondary:
        add(tx_date, "G&A", vendor, cat, amt, inv, desc)

    return txs


def _build_seed_subscriptions() -> list[tuple[str, str, str, int, float, float, int, str, str]]:
    """Build 10 enterprise SaaS subscriptions with price hikes (>15%) and >90d inactivity."""
    return [
        # Price hikes (>= 20%)
        ("Snowflake", "Engineering", "Data Warehouse", 45, 8100.0, 6200.0, 1, "2026-01-15", "review"),
        ("Salesforce Enterprise", "Sales", "CRM", 68, 6588.0, 5400.0, 0, "2026-02-01", "review"),
        ("Datadog Pro", "Engineering", "Observability", 38, 4560.0, 3800.0, 2, "2026-01-28", "review"),
        # Inactive / Zombie licenses (> 90 days unused)
        ("Tableau Legacy Seats", "Operations", "BI Analytics", 22, 2400.0, 2400.0, 128, "2026-01-10", "at_risk"),
        ("Marketo Legacy Add-on", "Marketing", "MarTech", 15, 1850.0, 1850.0, 112, "2026-01-20", "at_risk"),
        ("Miro Unused Org Tier", "G&A", "Collaboration", 30, 950.0, 950.0, 96, "2026-02-14", "at_risk"),
        # Healthy active subscriptions
        ("HubSpot Marketing Hub", "Marketing", "MarTech", 28, 3200.0, 3150.0, 1, "2026-03-01", "active"),
        ("GitHub Enterprise", "Engineering", "DevTools", 54, 2100.0, 2100.0, 0, "2026-04-01", "active"),
        ("Slack Business+", "G&A", "Collaboration", 140, 1900.0, 1900.0, 0, "2026-03-15", "active"),
        ("Okta Workforce Identity", "G&A", "Security & IAM", 140, 1600.0, 1600.0, 0, "2026-05-01", "active"),
    ]


def _seed_database(conn: sqlite3.Connection) -> None:
    """Populate all core tables with realistic FY2025 enterprise data."""
    budgets = _build_seed_budgets()
    conn.executemany(
        "INSERT INTO budgets (month, department, budget_amount) VALUES (?, ?, ?)",
        budgets,
    )

    txs = _build_seed_transactions()
    conn.executemany(
        """
        INSERT INTO transactions (
            tx_date, month, department, vendor, category,
            amount, invoice_id, description, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        txs,
    )

    subs = _build_seed_subscriptions()
    conn.executemany(
        """
        INSERT INTO subscriptions (
            vendor, department, category, seats, monthly_cost,
            prev_monthly_cost, last_used_days, renewal_date, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        subs,
    )

    # Seed baseline ERP ingestion run so ETL history is populated on launch
    conn.execute(
        """
        INSERT INTO etl_runs (
            ran_at, filename, status, rows_total, rows_inserted,
            duplicates_skipped, anomalies_flagged, duration_ms, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2025-12-01T08:00:05Z",
            "fy2025_erp_ledger_baseline.csv",
            "completed",
            len(txs),
            len(txs),
            0,
            6,
            18.4,
            "Initial FY2025 ERP ledger baseline loaded across 5 departments and 12 fiscal months.",
        ),
    )

    # Seed an initial completed workflow run aligned with live workflow webhook schema
    initial_steps = [
        {
            "name": "Ledger Completeness Check",
            "status": "passed",
            "detail": f"Verified {len(txs)} ledger postings across 12 fiscal periods and 5 departments.",
            "duration_ms": 1.4,
        },
        {
            "name": "Budget vs. Actual Variance Engine",
            "status": "passed",
            "detail": "Computed FY2025 variance & Q4-weighted EAC; flagged Engineering and Marketing >5% over budget.",
            "duration_ms": 2.1,
        },
        {
            "name": "Duplicate Invoice & Spend Spike Audit",
            "status": "passed",
            "detail": "Detected 2 duplicate ERP invoices ($32,650 exposure) and 4 statistical spend outliers.",
            "duration_ms": 2.6,
        },
        {
            "name": "Executive Close Brief & Slack Dispatch",
            "status": "passed",
            "detail": "Compiled CFO close memo and formatted #finops-alerts webhook payload.",
            "duration_ms": 1.1,
        },
    ]
    initial_md = (
        "# PulseOps AI — Monthly Financial Close Package (Baseline)\n\n"
        "- **Status:** Completed baseline close audit\n"
        "- **Priority Actions:** Review Engineering cloud/ML spikes (`INV-2025-0915`, `INV-2025-1108`), "
        "recover duplicate invoices (`INV-2025-0742`, `INV-2025-1019`), and reclaim inactive SaaS licenses.\n"
    )
    initial_summary = (
        "Baseline FY2025 close completed: Engineering & Marketing flagged over budget; 6 active anomalies."
    )
    initial_metrics = {
        "Total Actual": "$1,819,000.00",
        "Net Variance": "+$79,000.00 (+4.54%)",
        "Active Anomalies": "6",
        "SaaS Savings Opp": "$108,576.00/yr",
    }
    initial_webhook = {
        "channel": "#finops-alerts",
        "username": "PulseOps AI Automation",
        "workflow_id": "monthly_close",
        "workflow_name": "Monthly Financial Close & Variance Package",
        "timestamp": "2025-12-01T08:05:00Z",
        "severity": "high",
        "summary": initial_summary,
        "metrics": initial_metrics,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "PulseOps AI · Monthly Financial Close & Variance Package",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Executive Summary:* {initial_summary}",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}:*\n{v}"}
                    for k, v in initial_metrics.items()
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Top Action Items:*\n"
                        "• Engineering: +11.34% (+$65,300.00)\n"
                        "• Marketing: +7.58% (+$27,300.00)\n"
                        "• INV-2025-0742 (AWS Cloud Services): $18,450.00 [duplicate_invoice]"
                    ),
                },
            },
        ],
    }
    conn.execute(
        """
        INSERT INTO workflow_runs (
            workflow_id, workflow_name, ran_at, status, duration_ms,
            params_json, summary, steps_json, output_markdown, webhook_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "monthly_close",
            "Monthly Financial Close & Variance Package",
            "2025-12-01T08:05:00Z",
            "completed",
            7.2,
            json.dumps({"variance_threshold_pct": 5.0, "z_threshold": 2.0}),
            initial_summary,
            json.dumps(initial_steps),
            initial_md,
            json.dumps(initial_webhook),
        ),
    )


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Enable WAL mode once if not already enabled."""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    if not row or str(row[0]).lower() != "wal":
        conn.execute("PRAGMA journal_mode = WAL")


def _ensure_schema_in_tx(conn: sqlite3.Connection) -> None:
    """Create tables and indexes inside an active transaction (never calling executescript)."""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)


def init_db(db_path: str | Path | None = None) -> Path:
    """Create tables if missing, enable WAL once, and seed enterprise demo data when empty."""
    resolved = resolve_db_path(db_path)
    with get_db_lock(resolved):
        conn = get_connection(resolved, read_only=False)
        conn.isolation_level = None
        try:
            _ensure_wal_mode(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema_in_tx(conn)
                budget_count = conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
                if budget_count == 0:
                    _seed_database(conn)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    return resolved


def reset_db(db_path: str | Path | None = None) -> Path:
    """Atomically clear all PulseOps tables, reset sequences, and reseed demo data without dropping tables."""
    resolved = resolve_db_path(db_path)
    with get_db_lock(resolved):
        conn = get_connection(resolved, read_only=False)
        conn.isolation_level = None
        try:
            _ensure_wal_mode(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema_in_tx(conn)
                for tbl in (
                    "eval_runs",
                    "workflow_runs",
                    "etl_runs",
                    "subscriptions",
                    "transactions",
                    "budgets",
                ):
                    conn.execute(f"DELETE FROM {tbl}")
                has_seq = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
                ).fetchone()
                if has_seq:
                    conn.execute(
                        "DELETE FROM sqlite_sequence WHERE name IN "
                        "('budgets','transactions','subscriptions','etl_runs','workflow_runs','eval_runs')"
                    )
                _seed_database(conn)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    return resolved


def get_table_counts(db_path: str | Path | None = None) -> dict[str, int]:
    """Return row counts for all PulseOps SQLite tables from a consistent read snapshot."""
    with open_readonly_snapshot(db_path) as conn:
        tables = (
            "budgets",
            "transactions",
            "subscriptions",
            "etl_runs",
            "workflow_runs",
            "eval_runs",
        )
        counts: dict[str, int] = {}
        for name in tables:
            row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {name}").fetchone()
            counts[name] = int(row["cnt"]) if row else 0
        return counts
