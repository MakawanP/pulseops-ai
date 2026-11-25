"""CSV ETL ingestion pipeline, schema validation, deduplication, anomaly tagging, and data explorer summary."""

from __future__ import annotations

import csv
import io
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.analytics import detect_spend_anomalies
from backend.database import (
    DEPARTMENTS,
    get_connection,
    get_db_lock,
    open_readonly_snapshot,
    utc_now_iso,
)

REQUIRED_CSV_COLUMNS = (
    "tx_date",
    "department",
    "vendor",
    "category",
    "amount",
    "invoice_id",
)

MAX_TX_AMOUNT = 1e12  # $1T upper bound to prevent float overflow / ledger poisoning

CANONICAL_DEPARTMENTS = {d.lower(): d for d in DEPARTMENTS}

PRESET_QUERIES: list[dict[str, str]] = [
    {
        "id": "dept_budget_vs_actual",
        "label": "Dept Budget vs Actuals (CTE)",
        "sql": (
            "WITH dept_budgets AS (\n"
            "  SELECT department, ROUND(SUM(budget_amount), 2) AS budget\n"
            "  FROM budgets GROUP BY department\n"
            "),\n"
            "dept_actuals AS (\n"
            "  SELECT department, ROUND(SUM(amount), 2) AS actual, COUNT(*) AS tx_count\n"
            "  FROM transactions GROUP BY department\n"
            ")\n"
            "SELECT b.department, b.budget, a.actual,\n"
            "       ROUND(a.actual - b.budget, 2) AS variance,\n"
            "       ROUND(((a.actual - b.budget) / b.budget) * 100.0, 2) AS variance_pct,\n"
            "       a.tx_count\n"
            "FROM dept_budgets b\n"
            "JOIN dept_actuals a ON b.department = a.department\n"
            "ORDER BY variance_pct DESC;"
        ),
    },
    {
        "id": "duplicate_invoices",
        "label": "Duplicate Invoice Audit",
        "sql": (
            "SELECT MIN(TRIM(invoice_id)) AS invoice_id, MIN(department) AS department, MIN(vendor) AS vendor,\n"
            "       COUNT(*) AS postings,\n"
            "       ROUND(MAX(amount), 2) AS unit_amount,\n"
            "       ROUND(SUM(amount) - MIN(amount), 2) AS duplicate_exposure,\n"
            "       MIN(tx_date) AS first_seen, MAX(tx_date) AS last_seen\n"
            "FROM transactions\n"
            "GROUP BY UPPER(TRIM(invoice_id))\n"
            "HAVING COUNT(*) > 1\n"
            "ORDER BY duplicate_exposure DESC;"
        ),
    },
    {
        "id": "top_vendors",
        "label": "Top 10 Vendors by Spend",
        "sql": (
            "SELECT vendor, department, COUNT(*) AS invoices,\n"
            "       ROUND(SUM(amount), 2) AS total_spend,\n"
            "       ROUND(AVG(amount), 2) AS avg_invoice\n"
            "FROM transactions\n"
            "GROUP BY vendor, department\n"
            "ORDER BY total_spend DESC\n"
            "LIMIT 10;"
        ),
    },
    {
        "id": "monthly_burn",
        "label": "Monthly Budget vs Burn",
        "sql": (
            "SELECT b.month,\n"
            "       ROUND(SUM(b.budget_amount), 2) AS budget,\n"
            "       ROUND(COALESCE(t.actual, 0), 2) AS actual,\n"
            "       ROUND(COALESCE(t.actual, 0) - SUM(b.budget_amount), 2) AS variance\n"
            "FROM budgets b\n"
            "LEFT JOIN (\n"
            "  SELECT month, SUM(amount) AS actual FROM transactions GROUP BY month\n"
            ") t ON b.month = t.month\n"
            "GROUP BY b.month\n"
            "ORDER BY b.month ASC;"
        ),
    },
    {
        "id": "saas_watchdog",
        "label": "SaaS Price Hikes & Inactive",
        "sql": (
            "SELECT vendor, department, seats, monthly_cost, prev_monthly_cost,\n"
            "       ROUND(((monthly_cost - prev_monthly_cost) / prev_monthly_cost) * 100.0, 1) AS change_pct,\n"
            "       last_used_days, status\n"
            "FROM subscriptions\n"
            "WHERE last_used_days > 90\n"
            "   OR ((monthly_cost - prev_monthly_cost) / prev_monthly_cost) >= 0.15\n"
            "ORDER BY monthly_cost DESC;"
        ),
    },
    {
        "id": "largest_transactions",
        "label": "Top 15 Largest Invoices",
        "sql": (
            "SELECT tx_date, department, vendor, category, amount, invoice_id, description\n"
            "FROM transactions\n"
            "ORDER BY amount DESC\n"
            "LIMIT 15;"
        ),
    },
]


def _normalize_date(raw_date: str, row_num: int) -> str:
    """Validate and normalize a date string into YYYY-MM-DD."""
    cleaned = (raw_date or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Row {row_num}: invalid tx_date '{cleaned}' (expected YYYY-MM-DD).")


def _normalize_amount(raw_amount: str, row_num: int) -> float:
    """Validate and parse a bounded positive monetary amount (>= $0.01, <= $1e12)."""
    cleaned = (raw_amount or "").strip().replace("$", "").replace(",", "")
    if not cleaned:
        raise ValueError(f"Row {row_num}: amount is required.")
    try:
        val = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"Row {row_num}: invalid numeric amount '{raw_amount}'.") from exc
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"Row {row_num}: amount must be a positive finite number (got {raw_amount}).")
    if val > MAX_TX_AMOUNT:
        raise ValueError(
            f"Row {row_num}: amount exceeds maximum allowed value of ${MAX_TX_AMOUNT:,.0f} (got {raw_amount})."
        )
    rounded = round(val, 2)
    if rounded <= 0.0:
        raise ValueError(
            f"Row {row_num}: amount must be at least $0.01 after rounding (got {raw_amount})."
        )
    return rounded


def _normalize_department(raw_dept: str, row_num: int) -> str:
    cleaned = (raw_dept or "").strip()
    if not cleaned:
        raise ValueError(f"Row {row_num}: department is required.")
    return CANONICAL_DEPARTMENTS.get(cleaned.lower(), cleaned)


def ingest_csv_text(
    db_path: str | Path | None,
    filename: str,
    csv_text: str,
) -> dict[str, Any]:
    """Validate, atomically deduplicate, and load CSV transactions into SQLite, logging an etl_runs record."""
    started = time.perf_counter()
    safe_name = Path((filename or "upload.csv").strip() or "upload.csv").name

    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("CSV content cannot be empty.")

    # Handle UTF-8 BOM gracefully and convert any csv.Error (e.g. oversized field) into HTTP 400
    text = csv_text.lstrip("\ufeff")
    parsed_rows: list[dict[str, Any]] = []
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV file is missing a header row.")

        normalized_headers = {h.strip().lower(): h for h in reader.fieldnames if h is not None}
        missing = [col for col in REQUIRED_CSV_COLUMNS if col not in normalized_headers]
        if missing:
            raise ValueError(
                f"Missing required CSV columns: {', '.join(missing)}. "
                f"Required schema: {', '.join(REQUIRED_CSV_COLUMNS)}."
            )

        for row_num, raw_row in enumerate(reader, start=2):
            # Reject rows with extra unquoted columns instead of truncating or crashing
            if raw_row.get(None):
                raise ValueError(
                    f"Row {row_num}: unexpected extra column(s) found (check for unquoted commas)."
                )

            # Skip completely blank trailing lines safely
            if not any(
                (v.strip() if isinstance(v, str) else "")
                for k, v in raw_row.items()
                if k is not None
            ):
                continue

            def field(col_name: str) -> str:
                orig_key = normalized_headers.get(col_name)
                if not orig_key:
                    return ""
                raw_val = raw_row.get(orig_key)
                return raw_val.strip() if isinstance(raw_val, str) else ""

            tx_date = _normalize_date(field("tx_date"), row_num)
            department = _normalize_department(field("department"), row_num)
            vendor = field("vendor")
            category = field("category")
            invoice_id = field("invoice_id")
            description = field("description")

            if not vendor:
                raise ValueError(f"Row {row_num}: vendor is required.")
            if not category:
                raise ValueError(f"Row {row_num}: category is required.")
            if not invoice_id:
                raise ValueError(f"Row {row_num}: invoice_id is required.")

            amount = _normalize_amount(field("amount"), row_num)
            parsed_rows.append(
                {
                    "tx_date": tx_date,
                    "month": tx_date[:7],
                    "department": department,
                    "vendor": vendor,
                    "category": category,
                    "amount": amount,
                    "invoice_id": invoice_id,
                    "description": description,
                }
            )
    except csv.Error as exc:
        raise ValueError(f"Malformed CSV input: {exc}") from exc

    if not parsed_rows:
        raise ValueError("CSV file contains zero data rows.")

    now_iso = utc_now_iso()
    rows_total = len(parsed_rows)
    inserted_norm_ids: set[str] = set()
    duplicates_skipped = 0
    to_insert: list[tuple[Any, ...]] = []
    anomalies_detected: list[dict[str, Any]] = []

    # Atomic BEGIN IMMEDIATE transaction serialized via per-DB lock eliminates TOCTOU dedup races
    with get_db_lock(db_path):
        conn = get_connection(db_path, read_only=False)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_ids = {
                    str(r["norm_id"])
                    for r in conn.execute(
                        "SELECT DISTINCT UPPER(TRIM(invoice_id)) AS norm_id FROM transactions"
                    ).fetchall()
                }
                seen_batch: set[str] = set()

                for item in parsed_rows:
                    inv_id = item["invoice_id"].strip()
                    norm_inv = inv_id.upper()
                    if norm_inv in existing_ids or norm_inv in seen_batch:
                        duplicates_skipped += 1
                        continue
                    seen_batch.add(norm_inv)
                    inserted_norm_ids.add(norm_inv)
                    to_insert.append(
                        (
                            item["tx_date"],
                            item["month"],
                            item["department"],
                            item["vendor"],
                            item["category"],
                            item["amount"],
                            inv_id,
                            item["description"],
                            f"csv_upload:{safe_name}",
                            now_iso,
                        )
                    )

                if to_insert:
                    conn.executemany(
                        """
                        INSERT INTO transactions (
                            tx_date, month, department, vendor, category,
                            amount, invoice_id, description, source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        to_insert,
                    )

                # Detect statistical spend spikes atomically within the same transaction
                if inserted_norm_ids:
                    all_spikes = detect_spend_anomalies(db_path, z_threshold=2.0, conn=conn)
                    anomalies_detected = [
                        a for a in all_spikes if a["invoice_id"].strip().upper() in inserted_norm_ids
                    ]

                duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
                inserted_count = len(to_insert)
                summary_msg = (
                    f"Processed {rows_total} rows from {safe_name}: "
                    f"{inserted_count} inserted, {duplicates_skipped} duplicate(s) skipped, "
                    f"{len(anomalies_detected)} new spend anomaly(ies) flagged."
                )

                cur = conn.execute(
                    """
                    INSERT INTO etl_runs (
                        ran_at, filename, status, rows_total, rows_inserted,
                        duplicates_skipped, anomalies_flagged, duration_ms, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_iso,
                        safe_name,
                        "completed",
                        rows_total,
                        inserted_count,
                        duplicates_skipped,
                        len(anomalies_detected),
                        duration_ms,
                        summary_msg,
                    ),
                )
                run_id = int(cur.lastrowid)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    run_record = {
        "id": run_id,
        "ran_at": now_iso,
        "filename": safe_name,
        "status": "completed",
        "rows_total": rows_total,
        "rows_inserted": inserted_count,
        "duplicates_skipped": duplicates_skipped,
        "anomalies_flagged": len(anomalies_detected),
        "duration_ms": duration_ms,
        "message": summary_msg,
    }

    return {
        "ok": True,
        "run": run_record,
        "inserted_count": inserted_count,
        "duplicates_skipped": duplicates_skipped,
        "anomalies_detected": anomalies_detected,
    }


def get_data_summary(db_path: str | Path | None = None) -> dict[str, Any]:
    """Assemble GET /api/data/summary from a single consistent read snapshot."""
    with open_readonly_snapshot(db_path) as conn:
        table_names = (
            "budgets",
            "transactions",
            "subscriptions",
            "etl_runs",
            "workflow_runs",
            "eval_runs",
        )
        tables: list[dict[str, Any]] = []
        for tbl in table_names:
            cnt = int(conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0])
            col_rows = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            cols = [str(r["name"]) for r in col_rows]
            tables.append({"name": tbl, "row_count": cnt, "columns": cols})

        etl_rows = conn.execute(
            """
            SELECT id, ran_at, filename, status, rows_total, rows_inserted,
                   duplicates_skipped, anomalies_flagged, duration_ms, message
            FROM etl_runs
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        etl_runs = [dict(r) for r in etl_rows]

        tx_rows = conn.execute(
            """
            SELECT id, tx_date, month, department, vendor, category, amount,
                   invoice_id, description, source
            FROM transactions
            ORDER BY tx_date DESC, id DESC
            LIMIT 25
            """
        ).fetchall()
        recent_transactions = [dict(r) for r in tx_rows]

    return {
        "tables": tables,
        "etl_runs": etl_runs,
        "recent_transactions": recent_transactions,
        "preset_queries": PRESET_QUERIES,
    }
