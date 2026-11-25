"""Automated live AI & Data Reliability Eval Harness (12 deterministic SQL-anchored test cases)."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from backend.agent import ask_copilot
from backend.analytics import detect_duplicate_invoices, detect_spend_anomalies
from backend.database import (
    execute_readonly_sql,
    get_connection,
    get_db_lock,
    resolve_db_path,
    utc_now_iso,
    validate_readonly_sql,
)
from backend.etl import ingest_csv_text
from backend.workflows import run_workflow

SEEDED_SPIKE_INVOICE_IDS = frozenset(
    {
        "INV-2025-0915",
        "INV-2025-1108",
        "INV-2025-1124",
        "INV-2025-1033",
    }
)


def _run_case(
    case_id: str,
    category: str,
    name: str,
    fn: Callable[[], tuple[bool, str, str, str]],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        passed, expected, actual, detail = fn()
    except Exception as exc:  # noqa: BLE001
        passed = False
        expected = "No unhandled exception"
        actual = f"{exc.__class__.__name__}: {exc}"
        detail = f"Eval case raised unexpected error: {exc}"
    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return {
        "id": case_id,
        "category": category,
        "name": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
        "latency_ms": latency_ms,
        "detail": detail,
    }


def _fmt_signed(val: float) -> str:
    sign = "+" if val > 0 else ("-" if val < 0 else "")
    return f"{sign}${abs(val):,.2f}"


def run_eval_suite(db_path: str | Path | None = None, persist: bool = True) -> dict[str, Any]:
    """Execute all 12 deterministic reliability & safety eval cases and optionally persist to eval_runs."""
    resolved_db = resolve_db_path(db_path)

    # --- Category 1: Numerical Faithfulness (anchored against independent raw SQL queries) ---
    def eval_01() -> tuple[bool, str, str, str]:
        conn = get_connection(resolved_db, read_only=True)
        try:
            sql_budget = round(
                float(conn.execute("SELECT COALESCE(SUM(budget_amount), 0) FROM budgets").fetchone()[0]),
                2,
            )
            sql_actual = round(
                float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions").fetchone()[0]),
                2,
            )
        finally:
            conn.close()

        sql_variance = round(sql_actual - sql_budget, 2)
        expected_actual = f"${sql_actual:,.2f}"
        expected_budget = f"${sql_budget:,.2f}"
        expected_var = _fmt_signed(sql_variance)

        res = ask_copilot(
            resolved_db,
            "Give me a summary of total budget, total actual spend, and net variance.",
            provider="local",
        )
        ans = res["answer_markdown"]
        cit_map = {c["metric"]: c["value"] for c in res.get("citations", [])}
        has_actual = expected_actual in ans and cit_map.get("Total Actual Spend") == expected_actual
        has_budget = expected_budget in ans and cit_map.get("Total FY Budget") == expected_budget
        has_var = expected_var in ans
        passed = has_actual and has_budget and has_var
        return (
            passed,
            f"Raw SQL truth: {expected_actual} actual, {expected_budget} budget ({expected_var})",
            f"actual={has_actual}, budget={has_budget}, variance={has_var}",
            "Anchored Copilot narrative and citations against independent raw SQL SUM(amount) & SUM(budget_amount).",
        )

    def eval_02() -> tuple[bool, str, str, str]:
        conn = get_connection(resolved_db, read_only=True)
        try:
            eng_budget = round(
                float(
                    conn.execute(
                        "SELECT COALESCE(SUM(budget_amount), 0) FROM budgets WHERE department = 'Engineering'"
                    ).fetchone()[0]
                ),
                2,
            )
            eng_actual = round(
                float(
                    conn.execute(
                        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE department = 'Engineering'"
                    ).fetchone()[0]
                ),
                2,
            )
        finally:
            conn.close()

        eng_var = round(eng_actual - eng_budget, 2)
        eng_pct = round((eng_var / eng_budget) * 100.0, 2) if eng_budget > 0 else 0.0
        expected_status = (
            "over_budget"
            if (eng_budget == 0 and eng_actual > 0) or eng_pct > 5.0
            else ("watch" if eng_pct > 0.0 else "on_track")
        )
        expected_str = f"${eng_actual:,.2f}"
        expected_pct = f"{eng_pct:+.2f}%"

        res = ask_copilot(
            resolved_db,
            "Which departments are over budget and what is Engineering variance?",
            provider="local",
        )
        ans = res["answer_markdown"]
        # Anchor check to the Engineering line specifically (not matching status elsewhere in text)
        eng_lines = [line for line in ans.splitlines() if "Engineering" in line]
        line_matched = any(
            expected_str in line and expected_pct in line and expected_status in line
            for line in eng_lines
        )
        return (
            line_matched,
            f"Engineering line matches SQL truth: {expected_str} ({expected_pct}, {expected_status})",
            f"anchored_match={line_matched}",
            "Verified Engineering actuals, variance %, and status on its own line against raw SQL.",
        )

    def eval_03() -> tuple[bool, str, str, str]:
        conn = get_connection(resolved_db, read_only=True)
        try:
            row = conn.execute(
                """
                SELECT ROUND(
                    COALESCE(
                        SUM(
                            CASE
                                WHEN last_used_days > 90 THEN monthly_cost
                                WHEN prev_monthly_cost > 0
                                     AND ROUND(((monthly_cost - prev_monthly_cost) / prev_monthly_cost) * 100.0, 2) >= 15.0
                                    THEN CASE WHEN monthly_cost > prev_monthly_cost THEN monthly_cost - prev_monthly_cost ELSE 0.0 END
                                ELSE 0.0
                            END
                        ),
                        0.0
                    ) * 12.0,
                    2
                ) AS annual_savings
                FROM subscriptions
                """
            ).fetchone()
            sql_annual_opp = round(float(row["annual_savings"] or 0.0), 2)
        finally:
            conn.close()

        expected_opp = f"${sql_annual_opp:,.2f}"
        res = ask_copilot(
            resolved_db,
            "Summarize SaaS subscription savings, price hikes, and inactive licenses.",
            provider="local",
        )
        ans = res["answer_markdown"]
        passed = expected_opp in ans and any(
            c.get("metric") == "SaaS Annual Savings Opp" and c.get("value") == expected_opp
            for c in res.get("citations", [])
        )
        return (
            passed,
            f"Raw SQL annual SaaS savings citation & narrative = {expected_opp}",
            f"matched={passed} ({expected_opp})",
            "Anchored Copilot SaaS savings opportunity against independent SQL subscription query.",
        )

    # --- Category 2: SQL Safety ---
    def eval_04() -> tuple[bool, str, str, str]:
        blocked = False
        err_msg = ""
        try:
            execute_readonly_sql(resolved_db, "DROP TABLE transactions;")
        except ValueError as exc:
            blocked = True
            err_msg = str(exc)
        return (
            blocked,
            "ValueError rejecting DROP DDL statement",
            f"blocked={blocked}",
            err_msg or "DDL attack was not blocked!",
        )

    def eval_05() -> tuple[bool, str, str, str]:
        blocked = False
        err_msg = ""
        try:
            execute_readonly_sql(
                resolved_db, "SELECT * FROM budgets; DELETE FROM transactions;"
            )
        except ValueError as exc:
            blocked = True
            err_msg = str(exc)
        return (
            blocked,
            "ValueError rejecting stacked multi-statement '; DELETE'",
            f"blocked={blocked}",
            err_msg or "Multi-statement injection was not blocked!",
        )

    def eval_06() -> tuple[bool, str, str, str]:
        attack_payloads = (
            "PRAGMA table_info(transactions)",
            "SELECT * FROM pragma_database_list",
            "ATTACH DATABASE '/tmp/pwn.db' AS pwn",
            "UPDATE transactions SET amount = 1",
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT * FROM c",
        )
        blocked_attacks = []
        for payload in attack_payloads:
            try:
                execute_readonly_sql(resolved_db, payload)
                blocked_attacks.append(False)
            except ValueError:
                blocked_attacks.append(True)

        cte_res = execute_readonly_sql(
            resolved_db,
            "WITH cte AS (SELECT department, SUM(amount) AS total FROM transactions GROUP BY department) "
            "SELECT department, total FROM cte ORDER BY total DESC LIMIT 3",
        )
        quoted_semi_res = execute_readonly_sql(
            resolved_db,
            "SELECT invoice_id FROM transactions WHERE department = 'Engineering' LIMIT 2;",
        )
        cte_ok = cte_res["ok"] and cte_res["row_count"] == 3 and quoted_semi_res["ok"]
        passed = all(blocked_attacks) and cte_ok
        return (
            passed,
            f"PRAGMA/pragma_*/ATTACH/UPDATE/RECURSIVE blocked ({len(attack_payloads)}/{len(attack_payloads)}) & CTE/quoted ';' succeed",
            f"blocked={sum(blocked_attacks)}/{len(attack_payloads)}, cte_rows={cte_res['row_count']}",
            "Confirmed authorizer + validator block metadata/mutation/recursive attacks while allowing valid CTE & quoted SELECT.",
        )

    # --- Category 3: Anomaly Detection ---
    def eval_07() -> tuple[bool, str, str, str]:
        spikes = detect_spend_anomalies(resolved_db, z_threshold=2.0)
        flagged_ids = {s["invoice_id"] for s in spikes}
        required = {"INV-2025-0915", "INV-2025-1108", "INV-2025-1124"}
        hits = required & flagged_ids
        recall = len(hits) / len(required)
        passed = recall == 1.0
        return (
            passed,
            "Recall = 100% on seeded high-severity spikes (3/3)",
            f"Recall = {recall * 100:.0f}% ({len(hits)}/3)",
            f"Flagged spike IDs: {', '.join(sorted(flagged_ids))}",
        )

    def eval_08() -> tuple[bool, str, str, str]:
        # Full-dataset precision check across ALL normal seeded baseline transactions
        conn = get_connection(resolved_db, read_only=True)
        try:
            normal_rows = conn.execute(
                "SELECT DISTINCT invoice_id FROM transactions WHERE source = 'erp_seed'"
            ).fetchall()
        finally:
            conn.close()

        normal_baseline_ids = {
            str(r["invoice_id"]) for r in normal_rows
        } - set(SEEDED_SPIKE_INVOICE_IDS)

        spikes = detect_spend_anomalies(resolved_db, z_threshold=2.0)
        flagged_ids = {s["invoice_id"] for s in spikes}
        false_positives = normal_baseline_ids & flagged_ids
        passed = len(normal_baseline_ids) > 0 and len(false_positives) == 0
        return (
            passed,
            f"0 false positives across all {len(normal_baseline_ids)} non-spike seeded invoices",
            f"{len(false_positives)} FP / {len(normal_baseline_ids)} clean invoices",
            "Confirmed full-dataset precision: zero non-spike ERP baseline invoices falsely flagged under Z-score + IQR bounds.",
        )

    # --- Category 4: Duplicate Guard ---
    def eval_09() -> tuple[bool, str, str, str]:
        dups = detect_duplicate_invoices(resolved_db)
        dup_ids = {d["invoice_id"].upper() for d in dups}
        required = {"INV-2025-0742", "INV-2025-1019"}
        passed = required.issubset(dup_ids)
        return (
            passed,
            "Detects INV-2025-0742 and INV-2025-1019 duplicates",
            f"Found {', '.join(sorted(dup_ids))}",
            f"Duplicate ledger audit identified {len(dups)} duplicate invoice group(s).",
        )

    def eval_10() -> tuple[bool, str, str, str]:
        # Run live ETL deduplication test on an isolated WAL-safe SQLite backup copy
        with tempfile.TemporaryDirectory(prefix="pulseops_eval_") as tmpdir:
            tmp_db = Path(tmpdir) / "eval_copy.db"
            src_conn = get_connection(resolved_db, read_only=True)
            dst_conn = sqlite3.connect(str(tmp_db))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()

            sample_csv = (
                "tx_date,department,vendor,category,amount,invoice_id,description\n"
                "2025-12-26,Engineering,AWS Cloud Services,Cloud Infrastructure,18450.00,inv-2025-0742,Case-insensitive dup test\n"
                "2025-12-26,G&A,Okta,Security & IAM,13100.00,INV-EVAL-UNIQUE-1,Clean unique row\n"
            )
            res = ingest_csv_text(tmp_db, "eval_dedup.csv", sample_csv)
            passed = res["inserted_count"] == 1 and res["duplicates_skipped"] == 1
            return (
                passed,
                "inserted_count=1, duplicates_skipped=1 (case-insensitive)",
                f"inserted_count={res['inserted_count']}, duplicates_skipped={res['duplicates_skipped']}",
                "Verified ETL pipeline blocks case-insensitive duplicate invoice_id via WAL-safe backup snapshot.",
            )

    # --- Category 5: Output Schema ---
    def eval_11() -> tuple[bool, str, str, str]:
        res = ask_copilot(
            resolved_db,
            "Draft an executive variance brief highlighting anomalies and SaaS savings.",
            provider="local",
        )
        required_keys = {
            "ok",
            "mode",
            "mode_note",
            "answer_markdown",
            "citations",
            "tool_trace",
            "suggested_followups",
        }
        has_keys = required_keys.issubset(res.keys())
        trace_valid = (
            isinstance(res["tool_trace"], list)
            and len(res["tool_trace"]) >= 2
            and all(
                {"step", "tool", "input", "output_summary", "sql", "duration_ms"}.issubset(t.keys())
                for t in res["tool_trace"]
            )
            and any(t.get("tool") == "query_sql_readonly" and bool(t.get("sql")) for t in res["tool_trace"])
        )
        citations_valid = (
            isinstance(res["citations"], list)
            and len(res["citations"]) >= 2
            and all({"metric", "value", "source"}.issubset(c.keys()) for c in res["citations"])
        )
        passed = has_keys and trace_valid and citations_valid and bool(res["answer_markdown"].strip())
        return (
            passed,
            "Valid Copilot JSON schema with >=2 tool_trace steps, executed SQL & citations",
            f"steps={len(res['tool_trace'])}, citations={len(res['citations'])}, schema_ok={passed}",
            "Verified full Copilot contract including authentic SQL tool traces and grounded citations.",
        )

    def eval_12() -> tuple[bool, str, str, str]:
        wf = run_workflow(resolved_db, "monthly_close", persist=False)
        run = wf.get("run", {})
        run_keys = {
            "id",
            "workflow_id",
            "workflow_name",
            "ran_at",
            "status",
            "duration_ms",
            "params",
            "summary",
            "steps",
            "output_markdown",
            "webhook_payload",
        }
        has_run_keys = wf.get("ok") is True and run_keys.issubset(run.keys())
        steps = run.get("steps")
        steps_ok = (
            isinstance(steps, list)
            and len(steps) == 4
            and all(isinstance(s, dict) and s.get("status") == "passed" for s in steps)
        )
        webhook = run.get("webhook_payload", {})
        webhook_ok = isinstance(webhook, dict) and {
            "channel",
            "workflow_id",
            "workflow_name",
            "timestamp",
            "blocks",
            "metrics",
            "summary",
        }.issubset(webhook.keys())
        md_ok = isinstance(run.get("output_markdown"), str) and len(run["output_markdown"]) > 150
        passed = has_run_keys and steps_ok and webhook_ok and md_ok
        passed_steps = sum(1 for s in (steps or []) if isinstance(s, dict) and s.get("status") == "passed")
        return (
            passed,
            "Valid workflow run payload, 4 passed steps, Markdown brief & Slack blocks",
            f"passed_steps={passed_steps}/4, webhook_blocks={len(webhook.get('blocks', []))}",
            "Verified workflow automation schema, all step statuses == 'passed', and Slack Block Kit webhook structure.",
        )

    cases = [
        _run_case("EVAL-01", "Numerical Faithfulness", "Copilot Total Spend & Budget Grounding", eval_01),
        _run_case("EVAL-02", "Numerical Faithfulness", "Department Variance & Over-Budget Accuracy", eval_02),
        _run_case("EVAL-03", "Numerical Faithfulness", "SaaS Annual Savings Grounding", eval_03),
        _run_case("EVAL-04", "SQL Safety", "Block Destructive DROP TABLE Injection", eval_04),
        _run_case("EVAL-05", "SQL Safety", "Block Multi-Statement '; DELETE' Attack", eval_05),
        _run_case("EVAL-06", "SQL Safety", "Block PRAGMA/RECURSIVE & Allow CTE", eval_06),
        _run_case("EVAL-07", "Anomaly Detection", "Seeded Spend Spike Recall (100%)", eval_07),
        _run_case("EVAL-08", "Anomaly Detection", "Full-Dataset Baseline Precision (0 FP)", eval_08),
        _run_case("EVAL-09", "Duplicate Guard", "ERP Ledger Duplicate Invoice Audit", eval_09),
        _run_case("EVAL-10", "Duplicate Guard", "ETL Upload Deduplication Gatekeeper", eval_10),
        _run_case("EVAL-11", "Output Schema", "Copilot Contract & Tool-Trace Validity", eval_11),
        _run_case("EVAL-12", "Output Schema", "Workflow Brief & Webhook JSON Schema", eval_12),
    ]

    total = len(cases)
    passed = sum(1 for c in cases if c["passed"])
    pass_rate = round((passed / total) * 100.0, 2) if total else 0.0
    avg_latency_ms = round(sum(c["latency_ms"] for c in cases) / total, 2) if total else 0.0

    categories: dict[str, dict[str, int]] = {}
    for c in cases:
        cat = categories.setdefault(c["category"], {"passed": 0, "total": 0})
        cat["total"] += 1
        if c["passed"]:
            cat["passed"] += 1

    ran_at = utc_now_iso()
    if persist:
        with get_db_lock(resolved_db):
            conn = get_connection(resolved_db, read_only=False)
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT INTO eval_runs (
                            ran_at, total, passed, pass_rate, avg_latency_ms,
                            categories_json, cases_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ran_at,
                            total,
                            passed,
                            pass_rate,
                            avg_latency_ms,
                            json.dumps(categories, allow_nan=False),
                            json.dumps(cases, allow_nan=False),
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    return {
        "ok": True,
        "has_run": True,
        "ran_at": ran_at,
        "total": total,
        "passed": passed,
        "pass_rate": pass_rate,
        "avg_latency_ms": avg_latency_ms,
        "categories": categories,
        "cases": cases,
    }


def get_latest_eval(db_path: str | Path | None = None) -> dict[str, Any]:
    """Strictly read-only lookup of the latest eval run from eval_runs (never writes on GET)."""
    resolved_db = resolve_db_path(db_path)
    conn = get_connection(resolved_db, read_only=True)
    try:
        row = conn.execute(
            """
            SELECT ran_at, total, passed, pass_rate, avg_latency_ms,
                   categories_json, cases_json
            FROM eval_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {
            "ok": True,
            "has_run": False,
            "ran_at": None,
            "total": 0,
            "passed": 0,
            "pass_rate": None,
            "avg_latency_ms": None,
            "categories": {},
            "cases": [],
        }

    return {
        "ok": True,
        "has_run": True,
        "ran_at": str(row["ran_at"]),
        "total": int(row["total"]),
        "passed": int(row["passed"]),
        "pass_rate": float(row["pass_rate"]),
        "avg_latency_ms": float(row["avg_latency_ms"]),
        "categories": json.loads(row["categories_json"]),
        "cases": json.loads(row["cases_json"]),
    }
