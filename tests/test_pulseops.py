"""Comprehensive automated pytest suite for PulseOps AI.

All tests execute against isolated tmp_path SQLite databases and ephemeral local
HTTP servers bound to port 0 so data/pulseops.db is never created or mutated.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

# Ensure project root is importable regardless of pytest invocation directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

def http_request(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> tuple[int, dict[str, str], bytes, Any]:
    """Perform an HTTP request using stdlib urllib and return (status, headers, raw, parsed_json)."""
    url = f"{base_url}{path}"
    data: bytes | None = raw_body
    req_headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        ct = "application/json" if content_type is None else content_type
        if ct:
            req_headers["Content-Type"] = ct
    elif raw_body is not None:
        ct = "application/json" if content_type is None else content_type
        if ct:
            req_headers["Content-Type"] = ct
    elif content_type:
        req_headers["Content-Type"] = content_type

    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp_headers, raw, _try_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, resp_headers, raw, _try_json(raw)


def _try_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _start_ephemeral_server(db_path: Path):
    """Spin up PulseOps HTTP server bound to an ephemeral localhost port (port 0)."""
    import app as pulseops_app

    server = pulseops_app.create_server("127.0.0.1", 0, str(db_path))
    host, port = server.server_address[:2]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()
    return server, thread, f"http://{host}:{port}"


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    """Path to a fresh isolated SQLite DB in tmp_path (never touches data/pulseops.db)."""
    return tmp_path / "pulseops_test.db"


@pytest.fixture()
def live_server(isolated_db: Path):
    """Function-scoped ephemeral server backed by a clean isolated SQLite DB."""
    server, thread, base_url = _start_ephemeral_server(isolated_db)
    try:
        yield base_url, isolated_db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _query_sqlite(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _exec_sqlite(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


class _FakeHTTPResponse:
    """Minimal context-manager mock for urllib.request.urlopen responses."""

    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# 1. Database Seeding, Configuration & Reset Integrity
# ---------------------------------------------------------------------------

def test_database_seeding_populates_all_core_tables(live_server):
    base_url, db_path = live_server
    status, _, _, body = http_request(base_url, "GET", "/api/health")
    assert status == 200
    assert body["status"] == "ok"
    assert Path(body["db_path"]).resolve() == db_path.resolve()

    tables = body["tables"]
    assert tables["budgets"] == 60
    assert tables["transactions"] == 106
    assert tables["subscriptions"] == 10
    assert tables["etl_runs"] >= 1
    assert tables["workflow_runs"] >= 1
    assert "eval_runs" in tables and tables["eval_runs"] == 0

    rows = _query_sqlite(
        db_path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
    )
    table_names = {r["name"] for r in rows}
    assert {"budgets", "transactions", "subscriptions", "etl_runs", "workflow_runs", "eval_runs"}.issubset(
        table_names
    )


def test_health_endpoint_reports_llm_provider_flags(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(base_url, "GET", "/api/health")
    assert status == 200
    providers = body["llm_providers"]
    for key in ("gemini", "openai", "anthropic"):
        assert key in providers
        assert isinstance(providers[key], bool)


def test_resolve_db_path_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend.database import resolve_db_path

    env_db = tmp_path / "env_override.db"
    monkeypatch.setenv("PULSEOPS_DB_PATH", str(env_db))
    assert resolve_db_path(None) == env_db.resolve()

    explicit_db = tmp_path / "explicit.db"
    assert resolve_db_path(explicit_db) == explicit_db.resolve()


def test_load_dotenv_sets_defaults_without_overriding_shell_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app as pulseops_app

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Comment line\n"
        "export PULSEOPS_TEST_FROM_DOTENV='from_file'\n"
        "PULSEOPS_TEST_PRECEDENCE=from_file\n"
        'PULSEOPS_TEST_INLINE=value_ok # trailing comment\n'
        "INVALID-KEY=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PULSEOPS_TEST_FROM_DOTENV", raising=False)
    monkeypatch.setenv("PULSEOPS_TEST_PRECEDENCE", "from_shell")
    monkeypatch.delenv("PULSEOPS_TEST_INLINE", raising=False)

    loaded = pulseops_app.load_dotenv(env_file)
    assert loaded["PULSEOPS_TEST_FROM_DOTENV"] == "from_file"
    assert loaded["PULSEOPS_TEST_INLINE"] == "value_ok"
    assert "INVALID-KEY" not in loaded

    import os

    assert os.environ["PULSEOPS_TEST_FROM_DOTENV"] == "from_file"
    assert os.environ["PULSEOPS_TEST_PRECEDENCE"] == "from_shell"
    assert os.environ["PULSEOPS_TEST_INLINE"] == "value_ok"


def test_seeded_data_covers_12_months_and_5_departments(live_server):
    base_url, _ = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200
    assert len(overview["monthly_trend"]) == 12
    assert len(overview["department_variance"]) == 5
    depts = {d["department"] for d in overview["department_variance"]}
    assert depts == {"Engineering", "Marketing", "Sales", "Operations", "G&A"}


def test_database_reset_is_idempotent(live_server):
    base_url, _ = live_server
    _, _, _, before = http_request(base_url, "GET", "/api/health")
    baseline_tables = dict(before["tables"])

    # Mutate state via CSV upload, workflow execution, and eval run
    _, _, sample_raw, _ = http_request(base_url, "GET", "/api/sample-csv")
    up_status, _, _, _ = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "sample.csv", "csv_text": sample_raw.decode("utf-8")},
    )
    assert up_status == 200
    wf_status, _, _, _ = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": "monthly_close", "params": {}},
    )
    assert wf_status == 200
    ev_status, _, _, _ = http_request(base_url, "POST", "/api/evals/run", {})
    assert ev_status == 200

    # Reset twice to prove strict idempotency
    for _ in range(2):
        r_status, _, _, r_body = http_request(base_url, "POST", "/api/data/reset", {})
        assert r_status == 200
        assert r_body["ok"] is True
        assert isinstance(r_body["message"], str) and r_body["message"]

    _, _, _, after = http_request(base_url, "GET", "/api/health")
    assert after["tables"] == baseline_tables


# ---------------------------------------------------------------------------
# 2. Deterministic Analytics Math
# ---------------------------------------------------------------------------

def test_overview_kpi_totals_match_sqlite_ground_truth(live_server):
    base_url, db_path = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200
    kpis = overview["kpis"]

    budget_row = _query_sqlite(db_path, "SELECT SUM(budget_amount) AS total FROM budgets")[0]
    tx_row = _query_sqlite(db_path, "SELECT SUM(amount) AS total FROM transactions")[0]
    expected_budget = float(budget_row["total"])
    expected_actual = float(tx_row["total"])
    expected_variance = expected_actual - expected_budget
    expected_pct = (expected_variance / expected_budget) * 100.0

    assert kpis["total_budget"] == pytest.approx(expected_budget, abs=0.01)
    assert kpis["total_actual"] == pytest.approx(expected_actual, abs=0.01)
    assert kpis["net_variance"] == pytest.approx(expected_variance, abs=0.01)
    assert kpis["variance_pct"] == pytest.approx(expected_pct, abs=0.02)
    assert kpis["total_eac"] > kpis["total_budget"]
    assert kpis["last_eval_score"] is None


def test_monthly_trend_sums_reconcile_with_kpis(live_server):
    base_url, _ = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200

    kpis = overview["kpis"]
    trend = overview["monthly_trend"]
    assert len(trend) == 12
    assert [m["month"] for m in trend] == [f"2025-{idx:02d}" for idx in range(1, 13)]

    assert sum(item["budget"] for item in trend) == pytest.approx(kpis["total_budget"], abs=0.01)
    assert sum(item["actual"] for item in trend) == pytest.approx(kpis["total_actual"], abs=0.01)

    for item in trend:
        assert item["variance"] == pytest.approx(item["actual"] - item["budget"], abs=0.01)


def test_department_variance_eac_formula_and_status_classification(live_server):
    base_url, db_path = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200

    kpis = overview["kpis"]
    depts = overview["department_variance"]
    assert len(depts) == 5
    assert sum(d["budget"] for d in depts) == pytest.approx(kpis["total_budget"], abs=0.01)
    assert sum(d["actual"] for d in depts) == pytest.approx(kpis["total_actual"], abs=0.01)
    assert sum(d["eac"] for d in depts) == pytest.approx(kpis["total_eac"], abs=0.02)

    by_dept = {d["department"]: d for d in depts}
    assert by_dept["Engineering"]["status"] == "over_budget"
    assert by_dept["Marketing"]["status"] == "over_budget"
    assert by_dept["Sales"]["status"] == "watch"
    assert by_dept["Operations"]["status"] == "on_track"
    assert by_dept["G&A"]["status"] == "on_track"

    # Independently verify Q4-weighted annualized EAC formula (floored at actuals) against SQLite
    for dept in depts:
        monthly_rows = _query_sqlite(
            db_path,
            "SELECT month, SUM(amount) AS m_actual FROM transactions WHERE department = ? GROUP BY month ORDER BY month",
            (dept["department"],),
        )
        monthly_vals = [float(r["m_actual"]) for r in monthly_rows]
        actual = sum(monthly_vals)
        q4_annualized = (sum(monthly_vals[-3:]) / 3.0) * 12.0
        expected_eac = round(max(0.6 * actual + 0.4 * q4_annualized, actual), 2)

        assert dept["variance"] == pytest.approx(dept["actual"] - dept["budget"], abs=0.01)
        assert dept["variance_pct"] == pytest.approx((dept["variance"] / dept["budget"]) * 100.0, abs=0.02)
        assert dept["eac"] == pytest.approx(expected_eac, abs=0.02)


def test_anomaly_detection_identifies_seeded_spend_spikes(live_server):
    base_url, _ = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200

    anomalies = overview["anomalies"]
    assert overview["kpis"]["anomaly_count"] == len(anomalies)
    required_keys = {"id", "type", "severity", "department", "vendor", "amount", "detail", "tx_date", "invoice_id"}
    for item in anomalies:
        assert required_keys.issubset(item.keys())
        assert item["severity"] in {"high", "medium"}
        assert item["amount"] > 0

    spike_ids = {a["invoice_id"] for a in anomalies if a["type"] == "spend_spike"}
    assert {"INV-2025-0915", "INV-2025-1108", "INV-2025-1124", "INV-2025-1033"}.issubset(spike_ids)


def test_anomaly_detection_identifies_seeded_duplicate_invoices(live_server):
    base_url, _ = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200

    dup_items = [a for a in overview["anomalies"] if a["type"] == "duplicate_invoice"]
    assert overview["kpis"]["duplicate_count"] == len(dup_items) == 2
    assert {d["invoice_id"] for d in dup_items} == {"INV-2025-0742", "INV-2025-1019"}
    assert sum(d["amount"] for d in dup_items) == pytest.approx(32650.0, abs=0.01)


def test_subscription_watchdog_catches_price_hikes_and_inactivity(live_server):
    base_url, _ = live_server
    status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert status == 200

    subs = overview["subscriptions"]
    assert len(subs) == 10
    assert overview["kpis"]["subscription_savings_opp"] == pytest.approx(108576.0, abs=0.01)

    required_keys = {
        "id",
        "vendor",
        "department",
        "monthly_cost",
        "prev_monthly_cost",
        "price_change_pct",
        "last_used_days",
        "flag",
        "status",
    }
    for sub in subs:
        assert required_keys.issubset(sub.keys())
        if sub["prev_monthly_cost"] > 0:
            expected_pct = ((sub["monthly_cost"] - sub["prev_monthly_cost"]) / sub["prev_monthly_cost"]) * 100.0
            assert sub["price_change_pct"] == pytest.approx(expected_pct, abs=0.05)

    price_hikes = [s for s in subs if s["flag"] == "price_hike"]
    inactive = [s for s in subs if s["flag"] == "inactive_90d"]
    assert len(price_hikes) == 3
    assert len(inactive) == 3


# ---------------------------------------------------------------------------
# 3. CSV ETL Ingestion & Data Summary
# ---------------------------------------------------------------------------

def test_sample_csv_endpoint_serves_required_columns(live_server):
    base_url, _ = live_server
    status, headers, raw, _ = http_request(base_url, "GET", "/api/sample-csv")
    assert status == 200
    assert "text/csv" in headers.get("content-type", "").lower()
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 15  # header + 14 rows
    header = {col.strip() for col in lines[0].split(",")}
    assert {"tx_date", "department", "vendor", "category", "amount", "invoice_id"}.issubset(header)


def test_csv_upload_inserts_new_skips_duplicates_and_flags_anomalies(live_server):
    base_url, _ = live_server
    _, _, _, before_health = http_request(base_url, "GET", "/api/health")
    before_tx = before_health["tables"]["transactions"]
    before_etl = before_health["tables"]["etl_runs"]

    _, _, sample_raw, _ = http_request(base_url, "GET", "/api/sample-csv")
    csv_text = sample_raw.decode("utf-8")

    status, _, _, result = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "q4_upload_sample.csv", "csv_text": csv_text},
    )
    assert status == 200
    assert result["ok"] is True
    assert result["inserted_count"] == 12
    assert result["duplicates_skipped"] == 2
    flagged_ids = {a["invoice_id"] for a in result["anomalies_detected"]}
    assert {"INV-2025-1288", "INV-2025-1291"}.issubset(flagged_ids)
    assert result["run"]["filename"] == "q4_upload_sample.csv"
    assert result["run"]["rows_inserted"] == 12
    assert result["run"]["duplicates_skipped"] == 2

    _, _, _, after_health = http_request(base_url, "GET", "/api/health")
    assert after_health["tables"]["transactions"] == before_tx + 12
    assert after_health["tables"]["etl_runs"] == before_etl + 1


def test_csv_upload_normalizes_formats_and_deduplicates_within_batch(live_server):
    base_url, _ = live_server
    csv_text = (
        "\ufefftx_date,department,vendor,category,amount,invoice_id,description\n"
        "12/27/2025,engineering,Cloudflare,Security,\"$4,250.50\",INV-BATCH-001,WAF renewal\n"
        "2025-12-27,Engineering,Cloudflare,Security,4250.50,INV-BATCH-001,Intra-batch duplicate\n"
    )
    status, _, _, result = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "batch_norm.csv", "csv_text": csv_text},
    )
    assert status == 200
    assert result["ok"] is True
    assert result["inserted_count"] == 1
    assert result["duplicates_skipped"] == 1

    # Confirm canonical department & ISO date stored cleanly
    q_status, _, _, q_body = http_request(
        base_url,
        "POST",
        "/api/sql/query",
        {"sql": "SELECT tx_date, department, amount FROM transactions WHERE invoice_id = 'INV-BATCH-001'"},
    )
    assert q_status == 200
    assert q_body["rows"] == [["2025-12-27", "Engineering", 4250.5]]


def test_csv_upload_reupload_skips_all_duplicates(live_server):
    base_url, _ = live_server
    _, _, sample_raw, _ = http_request(base_url, "GET", "/api/sample-csv")
    csv_text = sample_raw.decode("utf-8")

    s1, _, _, r1 = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "pass1.csv", "csv_text": csv_text},
    )
    assert s1 == 200
    total_rows = r1["inserted_count"] + r1["duplicates_skipped"]

    s2, _, _, r2 = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "pass2.csv", "csv_text": csv_text},
    )
    assert s2 == 200
    assert r2["inserted_count"] == 0
    assert r2["duplicates_skipped"] == total_rows


def test_csv_upload_missing_required_columns_returns_400(live_server):
    base_url, _ = live_server
    bad_csv = "tx_date,department,vendor,amount\n2025-12-01,Engineering,AWS,1200.00\n"
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "bad_schema.csv", "csv_text": bad_csv},
    )
    assert status == 400
    assert body["ok"] is False
    assert "Missing required CSV columns" in body["error"]


@pytest.mark.parametrize(
    "bad_csv",
    [
        "",
        "   \n  ",
        "tx_date,department,vendor,category,amount,invoice_id\n",
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,NOT_A_NUMBER,INV-BAD-01\n",
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,-500,INV-BAD-02\n",
        "tx_date,department,vendor,category,amount,invoice_id\nnot-a-date,Engineering,AWS,Cloud,500,INV-BAD-03\n",
    ],
)
def test_csv_upload_empty_or_malformed_returns_400(live_server, bad_csv):
    base_url, _ = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "malformed.csv", "csv_text": bad_csv},
    )
    assert status == 400
    assert body["ok"] is False
    assert body["error"]


def test_data_summary_returns_tables_etl_runs_and_executable_presets(live_server):
    base_url, _ = live_server
    status, _, _, summary = http_request(base_url, "GET", "/api/data/summary")
    assert status == 200
    assert len(summary["tables"]) >= 5
    assert len(summary["etl_runs"]) >= 1
    assert len(summary["recent_transactions"]) > 0
    presets = summary["preset_queries"]
    assert len(presets) >= 5

    # Every preset query must pass the read-only guard and return rows/columns
    for preset in presets:
        assert {"id", "label", "sql"}.issubset(preset.keys())
        q_status, _, _, q_body = http_request(
            base_url, "POST", "/api/sql/query", {"sql": preset["sql"]}
        )
        assert q_status == 200, f"Preset {preset['id']} failed: {q_body}"
        assert q_body["ok"] is True
        assert len(q_body["columns"]) >= 1
        assert q_body["row_count"] >= 1
        assert isinstance(q_body["truncated"], bool)


# ---------------------------------------------------------------------------
# 4. SQL Read-Only Safety Guard
# ---------------------------------------------------------------------------

def test_sql_query_valid_select_with_trailing_semicolon_succeeds(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/sql/query",
        {"sql": "SELECT invoice_id, department, vendor, amount FROM transactions LIMIT 5;"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["columns"] == ["invoice_id", "department", "vendor", "amount"]
    assert body["row_count"] == 5
    assert body["truncated"] is False
    assert len(body["rows"]) == 5
    assert isinstance(body["duration_ms"], (int, float)) and body["duration_ms"] >= 0


def test_sql_query_valid_with_cte_succeeds(live_server):
    base_url, _ = live_server
    cte_sql = (
        "WITH dept_spend AS ("
        "  SELECT department, ROUND(SUM(amount), 2) AS total_spend "
        "  FROM transactions GROUP BY department"
        ") "
        "SELECT department, total_spend FROM dept_spend ORDER BY total_spend DESC"
    )
    status, _, _, body = http_request(base_url, "POST", "/api/sql/query", {"sql": cte_sql})
    assert status == 200
    assert body["ok"] is True
    assert body["columns"] == ["department", "total_spend"]
    assert body["row_count"] == 5
    assert body["truncated"] is False


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DROP TABLE transactions",
        "DELETE FROM transactions",
        "UPDATE transactions SET amount = 0",
        "INSERT INTO transactions (tx_date, month, department, vendor, category, amount, invoice_id, created_at) VALUES ('2025-01-01','2025-01','Eng','X','Y',1,'Z','2025-01-01')",
        "ALTER TABLE transactions ADD COLUMN hacked TEXT",
        "CREATE TABLE hacked (id INTEGER)",
        "PRAGMA table_info(transactions)",
        "ATTACH DATABASE ':memory:' AS aux",
        "VACUUM",
        "REINDEX",
        "REPLACE INTO subscriptions (id, vendor) VALUES (999, 'Evil')",
        "SELECT * FROM transactions LIMIT 1; DROP TABLE transactions",
        "SELECT 1; DELETE FROM budgets;",
        "/* stealth comment */ DROP TABLE budgets",
        "SELECT 1; -- comment\nDELETE FROM transactions",
    ],
)
def test_sql_guard_blocks_unsafe_and_multi_statement_queries(live_server, unsafe_sql):
    base_url, _ = live_server
    _, _, _, before = http_request(base_url, "GET", "/api/health")

    status, _, _, body = http_request(base_url, "POST", "/api/sql/query", {"sql": unsafe_sql})
    assert status == 400, f"Expected 400 for unsafe SQL: {unsafe_sql}"
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"]

    # Confirm database remained completely unmodified
    _, _, _, after = http_request(base_url, "GET", "/api/health")
    assert after["tables"] == before["tables"]


def test_sql_query_rejects_empty_and_syntax_errors(live_server):
    base_url, _ = live_server
    for bad_sql in ("", "   ", "SELECT FROM WHERE"):
        status, _, _, body = http_request(base_url, "POST", "/api/sql/query", {"sql": bad_sql})
        assert status == 400
        assert body["ok"] is False
        assert body["error"]


# ---------------------------------------------------------------------------
# 5. AI Copilot Tool-Calling Engine
# ---------------------------------------------------------------------------

def test_copilot_variance_question_offline_grounded_to_db(live_server):
    base_url, _ = live_server
    _, _, _, overview = http_request(base_url, "GET", "/api/overview")
    kpis = overview["kpis"]
    top_over_dept = max(overview["department_variance"], key=lambda d: d["variance_pct"])

    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Which departments are over budget and what is our total variance?",
            "provider": "local",
            "api_key": "",
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "local_grounded"
    assert isinstance(body["mode_note"], str) and body["mode_note"]
    assert len(body["tool_trace"]) >= 2
    assert len(body["citations"]) >= 3
    assert len(body["suggested_followups"]) >= 1

    for step in body["tool_trace"]:
        assert {"step", "tool", "method", "input", "output_summary", "sql", "duration_ms"}.issubset(
            step.keys()
        )
    assert any(step["tool"] == "query_sql_readonly" and step.get("sql") for step in body["tool_trace"])
    assert any(step["tool"] == "compute_department_variance" for step in body["tool_trace"])

    # Numerical faithfulness against database ground truth
    answer = body["answer_markdown"]
    assert f"${kpis['total_budget']:,.2f}" in answer
    assert f"${kpis['total_actual']:,.2f}" in answer
    assert top_over_dept["department"] in answer
    assert f"{top_over_dept['variance_pct']:+.2f}%" in answer


def test_copilot_anomaly_question_offline_grounded(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Audit duplicate invoices and spend spikes across departments",
            "provider": "local",
            "api_key": "",
        },
    )
    assert status == 200
    assert body["ok"] is True
    tool_names = {s["tool"] for s in body["tool_trace"]}
    assert "detect_anomalies" in tool_names
    assert len(body["citations"]) >= 2
    assert "INV-2025-0742" in body["answer_markdown"]
    assert "INV-2025-0915" in body["answer_markdown"]


def test_copilot_subscription_question_offline_grounded(live_server):
    base_url, _ = live_server
    _, _, _, overview = http_request(base_url, "GET", "/api/overview")
    expected_savings = f"${overview['kpis']['subscription_savings_opp']:,.2f}"

    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Summarize SaaS subscription price hikes and unused licenses",
            "provider": "local",
            "api_key": "",
        },
    )
    assert status == 200
    assert body["ok"] is True
    tool_names = {s["tool"] for s in body["tool_trace"]}
    assert "check_subscriptions" in tool_names
    assert expected_savings in body["answer_markdown"]
    assert any(c["value"] == expected_savings for c in body["citations"])


def test_copilot_executive_brief_question_offline(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Draft a Q4 executive financial brief for the CFO",
            "provider": "local",
            "api_key": "",
        },
    )
    assert status == 200
    assert body["ok"] is True
    tool_names = {s["tool"] for s in body["tool_trace"]}
    assert "draft_executive_brief" in tool_names
    assert len(body["answer_markdown"]) > 100
    assert len(body["citations"]) >= 4


def test_copilot_handles_validation_and_external_provider_fallback(
    live_server, monkeypatch: pytest.MonkeyPatch
):
    base_url, _ = live_server
    for env_key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env_key, raising=False)

    # Empty question -> 400
    bad_status, _, _, bad_body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "   ", "provider": "local", "api_key": ""},
    )
    assert bad_status == 400
    assert bad_body["ok"] is False

    # Unsupported provider -> 400
    prov_status, _, _, prov_body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "What is our variance?", "provider": "unsupported_llm", "api_key": ""},
    )
    assert prov_status == 400
    assert prov_body["ok"] is False

    # External provider with missing key -> clean local_fallback
    fb_status, _, _, fb_body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "What is our budget variance?", "provider": "openai", "api_key": ""},
    )
    assert fb_status == 200
    assert fb_body["ok"] is True
    assert fb_body["mode"] == "local_fallback"
    assert len(fb_body["tool_trace"]) >= 1


# ---------------------------------------------------------------------------
# 6. Automation Workflows
# ---------------------------------------------------------------------------

def test_workflows_catalog_exposes_three_pipelines(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(base_url, "GET", "/api/workflows")
    assert status == 200
    by_id = {wf["id"]: wf for wf in body["workflows"]}
    assert set(by_id.keys()) == {"monthly_close", "anomaly_watchdog", "exec_brief"}
    for wf in body["workflows"]:
        assert {"id", "name", "description", "schedule", "default_params", "steps"}.issubset(wf.keys())
        assert len(wf["steps"]) == 4

    # Verify exact default_params contract per workflow
    assert by_id["monthly_close"]["default_params"] == {"variance_threshold_pct": 5.0, "z_threshold": 2.0}
    assert by_id["anomaly_watchdog"]["default_params"] == {"z_threshold": 2.0}
    assert "variance_threshold_pct" not in by_id["anomaly_watchdog"]["default_params"]
    assert by_id["exec_brief"]["default_params"] == {"variance_threshold_pct": 5.0, "z_threshold": 2.0}

    assert isinstance(body["history"], list) and len(body["history"]) >= 1
    seeded_wh = body["history"][0]["webhook_payload"]
    assert {"channel", "workflow_id", "workflow_name", "timestamp", "metrics", "blocks"}.issubset(
        seeded_wh.keys()
    )


@pytest.mark.parametrize(
    "workflow_id,params",
    [
        ("monthly_close", {"variance_threshold_pct": 5.0}),
        ("anomaly_watchdog", {"z_threshold": 2.0}),
        ("exec_brief", {"variance_threshold_pct": 4.0, "z_threshold": 2.0}),
    ],
)
def test_workflow_pipelines_execute_and_persist(live_server, workflow_id, params):
    base_url, db_path = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": workflow_id, "params": params},
    )
    assert status == 200
    assert body["ok"] is True

    run = body["run"]
    assert run["workflow_id"] == workflow_id
    assert run["status"] == "completed"
    assert run["id"] > 0
    assert isinstance(run["params"], dict) and "z_threshold" in run["params"]
    assert isinstance(run["duration_ms"], (int, float)) and run["duration_ms"] >= 0
    assert len(run["steps"]) == 4
    for step in run["steps"]:
        assert step["status"] == "passed"
        assert step["name"] and step["detail"]
    assert isinstance(run["output_markdown"], str) and len(run["output_markdown"]) > 100

    webhook = run["webhook_payload"]
    assert {"channel", "workflow_id", "workflow_name", "timestamp", "summary", "metrics", "blocks"}.issubset(
        webhook.keys()
    )
    assert webhook["workflow_id"] == workflow_id
    assert len(webhook["blocks"]) >= 3

    rows = _query_sqlite(db_path, "SELECT id, workflow_id, status FROM workflow_runs WHERE id = ?", (run["id"],))
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == workflow_id and rows[0]["status"] == "completed"

    _, _, _, catalog = http_request(base_url, "GET", "/api/workflows")
    assert any(h["id"] == run["id"] and h["workflow_id"] == workflow_id for h in catalog["history"])


def test_workflow_invalid_id_and_out_of_range_params_return_400(live_server):
    base_url, _ = live_server
    s1, _, _, b1 = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": "nonexistent_pipeline", "params": {}},
    )
    assert s1 == 400 and b1["ok"] is False

    s2, _, _, b2 = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": "monthly_close", "params": {"variance_threshold_pct": 999}},
    )
    assert s2 == 400 and b2["ok"] is False


# ---------------------------------------------------------------------------
# 7. Live Eval Harness
# ---------------------------------------------------------------------------

def test_evals_latest_is_strictly_readonly_on_empty_db(live_server):
    base_url, db_path = live_server
    status, _, _, latest = http_request(base_url, "GET", "/api/evals/latest")
    assert status == 200
    assert latest["ok"] is True
    assert latest["has_run"] is False
    assert latest["ran_at"] is None
    assert latest["total"] == 0
    assert latest["passed"] == 0
    assert latest["pass_rate"] is None
    assert latest["avg_latency_ms"] is None
    assert latest["categories"] == {}
    assert latest["cases"] == []

    # Verify zero rows were written to eval_runs on GET
    eval_rows = _query_sqlite(db_path, "SELECT COUNT(*) AS cnt FROM eval_runs")
    assert eval_rows[0]["cnt"] == 0

    _, _, _, ov = http_request(base_url, "GET", "/api/overview")
    assert ov["kpis"]["last_eval_score"] is None


def test_evals_run_achieves_100_percent_pass_rate(live_server):
    base_url, _ = live_server
    status, _, _, body = http_request(base_url, "POST", "/api/evals/run", {})
    assert status == 200
    assert body["ok"] is True
    assert body["has_run"] is True
    assert body["total"] == 12
    assert body["passed"] == 12
    assert body["pass_rate"] == pytest.approx(100.0, abs=1e-6)
    assert isinstance(body["avg_latency_ms"], (int, float)) and body["avg_latency_ms"] >= 0

    expected_categories = {
        "Numerical Faithfulness",
        "SQL Safety",
        "Anomaly Detection",
        "Duplicate Guard",
        "Output Schema",
    }
    assert expected_categories.issubset(body["categories"].keys())
    assert len(body["cases"]) == 12

    for case in body["cases"]:
        assert {"id", "category", "name", "passed", "expected", "actual", "latency_ms", "detail"}.issubset(
            case.keys()
        )
        assert case["passed"] is True, f"Eval case failed: {case}"


def test_evals_latest_and_overview_reflect_last_eval_score(live_server):
    base_url, _ = live_server
    _, _, _, latest_before = http_request(base_url, "GET", "/api/evals/latest")
    assert latest_before["has_run"] is False

    _, _, _, ov_before = http_request(base_url, "GET", "/api/overview")
    assert ov_before["kpis"]["last_eval_score"] is None

    run_status, _, _, run_body = http_request(base_url, "POST", "/api/evals/run", {})
    assert run_status == 200
    assert run_body["has_run"] is True
    assert run_body["total"] == 12

    latest_status, _, _, latest_body = http_request(base_url, "GET", "/api/evals/latest")
    assert latest_status == 200
    assert latest_body["ok"] is True
    assert latest_body["has_run"] is True
    assert latest_body["total"] == run_body["total"] == 12
    assert latest_body["passed"] == run_body["passed"] == 12
    assert latest_body["pass_rate"] == pytest.approx(100.0, abs=1e-6)

    ov_status, _, _, overview = http_request(base_url, "GET", "/api/overview")
    assert ov_status == 200
    assert overview["kpis"]["last_eval_score"] == pytest.approx(100.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 8. Static Frontend Hosting & HTTP Guardrails
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path,expected_ct_substr",
    [
        ("/", "text/html"),
        ("/styles.css", "text/css"),
        ("/app.js", "javascript"),
        ("/charts.js", "javascript"),
    ],
)
def test_static_frontend_assets_served(live_server, path, expected_ct_substr):
    base_url, _ = live_server
    status, headers, raw, _ = http_request(base_url, "GET", path)
    assert status == 200
    assert expected_ct_substr in headers.get("content-type", "").lower()
    assert len(raw) > 100


def test_unknown_api_endpoint_and_malformed_json_handled_cleanly(live_server):
    base_url, _ = live_server
    s404, _, _, b404 = http_request(base_url, "GET", "/api/this-route-does-not-exist")
    assert s404 == 404
    assert b404["ok"] is False

    s400, _, _, b400 = http_request(
        base_url, "POST", "/api/sql/query", raw_body=b"{not valid json"
    )
    assert s400 == 400
    assert b400["ok"] is False


def test_static_path_traversal_does_not_leak_backend_files(live_server):
    base_url, _ = live_server
    status, _, raw, _ = http_request(base_url, "GET", "/../app.py")
    assert status in (400, 403, 404)
    assert b"create_server" not in raw


# ---------------------------------------------------------------------------
# 9. P0–P2 Hardening Regressions: SQL Guard, Security, Concurrency, ETL,
#    External LLM Fallback, Analytics, Copilot & Workflows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "valid_sql",
    [
        "SELECT * FROM transactions WHERE department = 'Engineering';",
        "SELECT invoice_id, amount FROM transactions WHERE department = 'Engineering' AND vendor = 'AWS Cloud Services';",
        "SELECT invoice_id FROM transactions WHERE description != 'semi;colon' LIMIT 3;",
    ],
)
def test_sql_guard_allows_quoted_string_literals_with_trailing_semicolon(live_server, valid_sql):
    """Regression for semicolon offset bug when SQL contains single-quoted literals + trailing ';'."""
    base_url, _ = live_server
    status, _, _, body = http_request(base_url, "POST", "/api/sql/query", {"sql": valid_sql})
    assert status == 200, f"Valid SQL rejected: {valid_sql} -> {body}"
    assert body["ok"] is True
    assert body["row_count"] >= 1
    assert body["truncated"] is False


@pytest.mark.parametrize(
    "blocked_sql",
    [
        "SELECT * FROM pragma_database_list",
        "SELECT * FROM pragma_table_info('transactions')",
        "SELECT * FROM pragma_compile_options",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM sqlite_schema",
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT SUM(x) FROM c",
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 5) SELECT x FROM cnt",
        "SELECT zeroblob(100000000)",
        "SELECT randomblob(100000000)",
        "SELECT load_extension('evil')",
        "SELECT sqlite_version()",
        "SELECT printf('%s', 'disallowed_fn')",
    ],
)
def test_sql_guard_blocks_pragma_tvf_recursive_zeroblob_and_unlisted_functions(
    live_server, blocked_sql
):
    """Regression for pragma_* TVF bypass, WITH RECURSIVE DoS, zeroblob, and authorizer enforcement."""
    base_url, _ = live_server
    status, _, _, body = http_request(base_url, "POST", "/api/sql/query", {"sql": blocked_sql})
    assert status == 400, f"Expected 400 for {blocked_sql}, got {status}: {body}"
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"]


def test_sql_query_serializes_blob_and_non_finite_floats_cleanly(live_server):
    """Regression for BLOB TypeError 500 and invalid JSON Infinity on SELECT 1e999."""
    base_url, _ = live_server
    status, _, raw, body = http_request(
        base_url,
        "POST",
        "/api/sql/query",
        {"sql": "SELECT X'DEADBEEF' AS blob_val, 1e999 AS pos_inf, -1e999 AS neg_inf;"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["truncated"] is False
    assert body["rows"] == [["deadbeef", None, None]]

    # Strict RFC-8259 check: no non-standard NaN/Infinity tokens in raw response
    def _reject_constant(token: str) -> Any:
        raise AssertionError(f"Non-standard JSON constant emitted: {token}")

    strict_parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    assert strict_parsed["rows"] == [["deadbeef", None, None]]


def test_sql_query_truncated_boolean_reported_accurately(live_server):
    """Verify 'truncated' is True past row cap (500 via API / custom max_rows) and False at or below cap."""
    from backend.database import execute_readonly_sql

    base_url, db_path = live_server

    # Unit-level exact boundary checks
    res_trunc = execute_readonly_sql(db_path, "SELECT id FROM transactions ORDER BY id", max_rows=10)
    assert res_trunc["row_count"] == 10
    assert res_trunc["truncated"] is True

    res_exact = execute_readonly_sql(
        db_path, "SELECT id FROM transactions ORDER BY id LIMIT 10", max_rows=10
    )
    assert res_exact["row_count"] == 10
    assert res_exact["truncated"] is False

    # HTTP-level >500-row check (10 subscriptions x 60 budgets = 600 rows > 500 cap)
    s_over, _, _, b_over = http_request(
        base_url,
        "POST",
        "/api/sql/query",
        {"sql": "SELECT s.id AS sid, b.id AS bid FROM subscriptions s CROSS JOIN budgets b;"},
    )
    assert s_over == 200
    assert b_over["row_count"] == 500
    assert b_over["truncated"] is True

    # HTTP-level <=500-row check
    s_exact, _, _, b_exact = http_request(
        base_url,
        "POST",
        "/api/sql/query",
        {"sql": "SELECT s.id AS sid, b.id AS bid FROM subscriptions s CROSS JOIN budgets b LIMIT 500;"},
    )
    assert s_exact == 200
    assert b_exact["row_count"] == 500
    assert b_exact["truncated"] is False


def test_server_blocks_cross_origin_post_and_allows_loopback_origin(live_server):
    """Regression for cross-origin simple-POST CSRF guard (403 on external Origin)."""
    base_url, _ = live_server

    # Cross-origin POSTs must be blocked with 403
    for bad_origin in ("https://evil.example", "http://127.0.0.1.evil.example"):
        status, _, _, body = http_request(
            base_url,
            "POST",
            "/api/data/reset",
            {},
            headers={"Origin": bad_origin},
        )
        assert status == 403
        assert body["ok"] is False

    # Loopback Origins must succeed
    for good_origin in ("http://127.0.0.1:8080", "http://localhost:8080"):
        status, _, _, body = http_request(
            base_url,
            "POST",
            "/api/sql/query",
            {"sql": "SELECT 1 AS ok_val"},
            headers={"Origin": good_origin},
        )
        assert status == 200
        assert body["ok"] is True


def test_server_enforces_json_content_type_on_post(live_server):
    """Regression for preflight-free text/plain POST CSRF (415 Unsupported Media Type)."""
    base_url, _ = live_server

    s_txt, _, _, b_txt = http_request(
        base_url,
        "POST",
        "/api/data/reset",
        raw_body=b"{}",
        content_type="text/plain",
    )
    assert s_txt == 415
    assert b_txt["ok"] is False

    s_form, _, _, b_form = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        raw_body=b"question=hello",
        content_type="application/x-www-form-urlencoded",
    )
    assert s_form == 415
    assert b_form["ok"] is False

    # Missing Content-Type on POST -> 415
    s_none, _, _, b_none = http_request(
        base_url,
        "POST",
        "/api/data/reset",
        raw_body=b"{}",
        content_type="",
    )
    assert s_none == 415
    assert b_none["ok"] is False

    # Empty body with application/json Content-Type -> 200
    s_empty, _, _, b_empty = http_request(
        base_url,
        "POST",
        "/api/data/reset",
        raw_body=b"",
        content_type="application/json; charset=utf-8",
    )
    assert s_empty == 200
    assert b_empty["ok"] is True


def test_server_returns_413_on_oversized_body_and_501_on_chunked(
    live_server, monkeypatch: pytest.MonkeyPatch
):
    """Verify HTTP 413 for >MAX_BODY_BYTES, 404 before body checks on unknown POST routes, and 501 on chunked."""
    import app as pulseops_app

    base_url, _ = live_server

    # Route checked before body / Content-Type validation
    s404, _, _, b404 = http_request(
        base_url,
        "POST",
        "/api/unknown-post-route",
        raw_body=b"not json",
        content_type="text/plain",
    )
    assert s404 == 404
    assert b404["ok"] is False

    # Oversized body -> 413 (use small MAX_BODY_BYTES to avoid OS TCP reset races)
    monkeypatch.setattr(pulseops_app, "MAX_BODY_BYTES", 512)
    oversized = {"sql": "SELECT '" + ("A" * 1024) + "'"}
    s413, _, _, b413 = http_request(base_url, "POST", "/api/sql/query", oversized)
    assert s413 == 413
    assert b413["ok"] is False

    # Transfer-Encoding: chunked -> 501
    host_port = base_url.removeprefix("http://")
    host, port_s = host_port.split(":")
    conn = http.client.HTTPConnection(host, int(port_s), timeout=10)
    try:
        conn.putrequest("POST", "/api/data/reset")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"2\r\n{}\r\n0\r\n\r\n")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        assert resp.status == 501
        assert body["ok"] is False
    finally:
        conn.close()


def test_server_sets_security_headers(live_server):
    """Verify X-Frame-Options, Referrer-Policy, X-Content-Type-Options, and CSP headers."""
    base_url, _ = live_server
    for path in ("/", "/api/health"):
        status, headers, _, _ = http_request(base_url, "GET", path)
        assert status == 200
        assert headers.get("x-content-type-options") == "nosniff"
        assert headers.get("x-frame-options") == "DENY"
        assert headers.get("referrer-policy") == "no-referrer"
        csp = headers.get("content-security-policy", "")
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


def test_gemini_malformed_api_key_never_leaks_secret_and_falls_back(live_server):
    """Regression: malformed Gemini API key must fall back cleanly with 200 and never leak secret."""
    base_url, _ = live_server
    secret_key = "AIza-SECRET-999 with space"
    status, _, raw, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Which departments are over budget?",
            "provider": "gemini",
            "api_key": secret_key,
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "local_fallback"
    assert b"AIza-SECRET-999" not in raw
    assert "Engineering" in body["answer_markdown"]


def test_internal_server_error_never_exposes_raw_exception_text(
    live_server, monkeypatch: pytest.MonkeyPatch
):
    """Regression: HTTP 500 JSON error must stay generic and never leak internal exception strings."""
    import app as pulseops_app

    base_url, _ = live_server
    secret_exc = "LEAKED_SECRET_CONNECTION_STRING_XYZ"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret_exc)

    monkeypatch.setattr(pulseops_app, "get_overview", _boom)
    s_get, _, raw_get, b_get = http_request(base_url, "GET", "/api/overview")
    assert s_get == 500
    assert b_get == {"ok": False, "error": "Internal server error."}
    assert secret_exc.encode("utf-8") not in raw_get

    monkeypatch.setattr(pulseops_app, "execute_readonly_sql", _boom)
    s_post, _, raw_post, b_post = http_request(
        base_url, "POST", "/api/sql/query", {"sql": "SELECT 1"}
    )
    assert s_post == 500
    assert b_post == {"ok": False, "error": "Internal server error."}
    assert secret_exc.encode("utf-8") not in raw_post


def test_concurrent_reset_db_and_get_overview_zero_errors(isolated_db: Path):
    """Regression: atomic BEGIN IMMEDIATE reset_db() never exposes missing tables to WAL readers."""
    from backend.analytics import get_overview
    from backend.database import init_db, reset_db

    init_db(isolated_db)
    stop_event = threading.Event()
    reader_errors: list[Exception] = []
    writer_errors: list[Exception] = []
    reads_completed = [0, 0]

    def _reader(idx: int) -> None:
        while not stop_event.is_set() or reads_completed[idx] < 20:
            try:
                ov = get_overview(isolated_db)
                assert len(ov["department_variance"]) == 5
                assert ov["kpis"]["total_budget"] == pytest.approx(1_740_000.0, abs=0.01)
                reads_completed[idx] += 1
            except Exception as exc:  # noqa: BLE001
                reader_errors.append(exc)
                break
            if stop_event.is_set() and reads_completed[idx] >= 20:
                break

    def _writer() -> None:
        try:
            for _ in range(15):
                reset_db(isolated_db)
        except Exception as exc:  # noqa: BLE001
            writer_errors.append(exc)
        finally:
            stop_event.set()

    readers = [threading.Thread(target=_reader, args=(i,)) for i in range(2)]
    writer = threading.Thread(target=_writer)
    for t in readers:
        t.start()
    writer.start()
    writer.join(timeout=30)
    for t in readers:
        t.join(timeout=30)

    assert writer_errors == [], f"Writer errors during concurrent reset: {writer_errors}"
    assert reader_errors == [], f"Reader errors during concurrent reset: {reader_errors}"
    assert all(cnt >= 20 for cnt in reads_completed)


def test_concurrent_identical_csv_uploads_produce_zero_duplicates(live_server):
    """Regression: concurrent identical CSV uploads serialize atomically with 0 duplicate rows."""
    base_url, db_path = live_server
    csv_text = (
        "tx_date,department,vendor,category,amount,invoice_id,description\n"
        "2025-12-28,Engineering,Cloudflare,Security,6400.00,INV-RACE-9001,Concurrent upload check\n"
    )
    n_threads = 6
    barrier = threading.Barrier(n_threads)
    results: list[tuple[int, Any]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            status, _, _, body = http_request(
                base_url,
                "POST",
                "/api/data/upload",
                {"filename": f"race_{idx}.csv", "csv_text": csv_text},
            )
            with lock:
                results.append((status, body))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert len(results) == n_threads
    assert all(status == 200 and body["ok"] is True for status, body in results)
    assert sum(body["inserted_count"] for _, body in results) == 1
    assert sum(body["duplicates_skipped"] for _, body in results) == n_threads - 1

    rows = _query_sqlite(
        db_path,
        "SELECT COUNT(*) AS cnt FROM transactions WHERE UPPER(TRIM(invoice_id)) = 'INV-RACE-9001'",
    )
    assert rows[0]["cnt"] == 1


def test_csv_case_insensitive_dedup_and_adversarial_inputs_rejected(live_server):
    """Regression: case-insensitive invoice_id dedup + clean 400 rejection of 1e200, $0.001, extra commas."""
    base_url, _ = live_server

    # 1. Case-insensitive dedup against seeded INV-2025-0742
    ci_csv = (
        "tx_date,department,vendor,category,amount,invoice_id,description\n"
        "2025-12-29,Engineering,AWS Cloud Services,Cloud Infrastructure,18450.00,inv-2025-0742,Lowercase dup\n"
    )
    s_ci, _, _, b_ci = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "ci_dup.csv", "csv_text": ci_csv},
    )
    assert s_ci == 200
    assert b_ci["inserted_count"] == 0
    assert b_ci["duplicates_skipped"] == 1

    # 2. Adversarial inputs must all fail with 400 and never poison the database
    adversarial_csvs = [
        # Huge float poisoning attempt (1e200)
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,1e200,INV-HUGE-01\n",
        # Tiny amount rounding to $0.00
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,0.001,INV-TINY-01\n",
        # Non-finite amounts
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,NaN,INV-NAN-01\n",
        "tx_date,department,vendor,category,amount,invoice_id\n2025-12-01,Engineering,AWS,Cloud,Infinity,INV-INF-01\n",
        # Extra unquoted commas on blank row (previously AttributeError 500)
        "tx_date,department,vendor,category,amount,invoice_id,description\n,,,,,,,oops\n",
        # Extra unquoted column on populated row
        "tx_date,department,vendor,category,amount,invoice_id,description\n"
        "2025-12-01,Engineering,AWS,Cloud,500,INV-EXTRA-01,note,unexpected_col\n",
    ]
    for bad_csv in adversarial_csvs:
        status, _, _, body = http_request(
            base_url,
            "POST",
            "/api/data/upload",
            {"filename": "adversarial.csv", "csv_text": bad_csv},
        )
        assert status == 400, f"Expected 400 for adversarial CSV:\n{bad_csv}\ngot {status}: {body}"
        assert body["ok"] is False

    # Confirm ledger and overview remain completely healthy (HTTP 200, baseline totals intact)
    s_ov, _, _, ov = http_request(base_url, "GET", "/api/overview")
    assert s_ov == 200
    assert ov["kpis"]["total_budget"] == pytest.approx(1_740_000.0, abs=0.01)


@pytest.mark.parametrize(
    "provider,mock_behavior",
    [
        ("gemini", json.dumps({"candidates": []}).encode("utf-8")),
        ("openai", json.dumps({"choices": [{"message": {"content": None}}]}).encode("utf-8")),
        ("anthropic", json.dumps({"content": None}).encode("utf-8")),
        ("gemini", http.client.InvalidURL("URL can't contain control characters")),
    ],
)
def test_external_llm_malformed_payloads_and_invalid_url_fallback_cleanly(
    live_server,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    mock_behavior: bytes | Exception,
):
    """Regression: safety-filtered Gemini, null OpenAI/Anthropic content, and InvalidURL -> 200 local_fallback."""
    base_url, _ = live_server
    real_urlopen = urllib.request.urlopen

    def _mock_urlopen(req: Any, timeout: float = 15) -> Any:
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if url.startswith(base_url):
            return real_urlopen(req, timeout=timeout)
        if isinstance(mock_behavior, Exception):
            raise mock_behavior
        return _FakeHTTPResponse(mock_behavior)

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Which departments are over budget?",
            "provider": provider,
            "api_key": "test-valid-key-12345",
        },
    )
    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "local_fallback"
    assert "Engineering" in body["answer_markdown"]
    assert len(body["tool_trace"]) >= 2


def test_gemini_uses_header_not_query_param_and_honors_model_override(
    live_server, monkeypatch: pytest.MonkeyPatch
):
    """Verify Gemini sends key via x-goog-api-key header (never ?key=) and honors GEMINI_MODEL."""
    base_url, _ = live_server
    real_urlopen = urllib.request.urlopen
    captured: dict[str, Any] = {}

    def _mock_urlopen(req: Any, timeout: float = 15) -> Any:
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if url.startswith(base_url):
            return real_urlopen(req, timeout=timeout)
        captured["url"] = url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        payload = {"candidates": [{"content": {"parts": [{"text": "Grounded Gemini synthesis."}]}}]}
        return _FakeHTTPResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {
            "question": "Which departments are over budget?",
            "provider": "gemini",
            "api_key": "AIza-TEST-HEADER-KEY",
        },
    )
    assert status == 200
    assert body["mode"] == "gemini_synthesized"
    assert body["answer_markdown"] == "Grounded Gemini synthesis."
    assert "gemini-2.5-pro:generateContent" in captured["url"]
    assert "key=" not in captured["url"]
    assert "AIza-TEST-HEADER-KEY" not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == "AIza-TEST-HEADER-KEY"


def test_unbudgeted_department_classified_over_budget_and_sorted_first(live_server):
    """Regression: department with budget == 0 and actual > 0 must be classified over_budget."""
    base_url, _ = live_server
    csv_text = (
        "tx_date,department,vendor,category,amount,invoice_id,description\n"
        "2025-12-15,Security,CrowdStrike,Cybersecurity,250000.00,INV-SEC-0001,Unbudgeted EDR rollout\n"
    )
    s_up, _, _, _ = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "unbudgeted.csv", "csv_text": csv_text},
    )
    assert s_up == 200

    s_ov, _, _, ov = http_request(base_url, "GET", "/api/overview")
    assert s_ov == 200
    depts = ov["department_variance"]
    assert depts[0]["department"] == "Security"
    assert depts[0]["budget"] == 0.0
    assert depts[0]["actual"] == pytest.approx(250_000.0, abs=0.01)
    assert depts[0]["status"] == "over_budget"
    assert depts[0]["unbudgeted"] is True


def test_future_year_invoice_does_not_drop_fy2025_eac_below_actuals(live_server):
    """Regression: posting a small 2026 invoice must not drop FY2025 EAC below incurred actuals."""
    base_url, _ = live_server
    _, _, _, before = http_request(base_url, "GET", "/api/overview")
    eng_before = next(d for d in before["department_variance"] if d["department"] == "Engineering")

    csv_text = (
        "tx_date,department,vendor,category,amount,invoice_id,description\n"
        "2026-01-05,Engineering,AWS Cloud Services,Cloud Infrastructure,1000.00,INV-2026-0001,Early Jan accrual\n"
    )
    s_up, _, _, _ = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "next_fy.csv", "csv_text": csv_text},
    )
    assert s_up == 200

    _, _, _, after = http_request(base_url, "GET", "/api/overview")
    eng_after = next(d for d in after["department_variance"] if d["department"] == "Engineering")

    assert eng_after["actual"] == pytest.approx(eng_before["actual"] + 1000.0, abs=0.01)
    assert eng_after["eac"] >= eng_after["actual"]
    assert eng_after["eac"] >= eng_before["eac"]


def test_copilot_word_boundaries_prevent_ops_and_sales_hijacking_and_emit_authentic_sql(
    live_server,
):
    """Regression: 'FinOps'/'PulseOps'/'Salesforce' must not hijack scope; tool_trace emits authentic SQL only."""
    base_url, _ = live_server

    # 1. "FinOps overview of budget variance" -> all 5 departments, NOT Operations-only
    s1, _, _, b1 = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "Give me a FinOps overview of budget variance", "provider": "local", "api_key": ""},
    )
    assert s1 == 200
    for dept in ("Engineering", "Marketing", "Sales", "Operations", "G&A"):
        assert dept in b1["answer_markdown"]
    var_step = next(s for s in b1["tool_trace"] if s["tool"] == "compute_department_variance")
    assert var_step["input"]["department"] is None
    assert var_step["sql"] is None
    assert var_step["method"] == "python_analytics"

    sql_steps = [s for s in b1["tool_trace"] if s["tool"] == "query_sql_readonly"]
    assert len(sql_steps) >= 1
    assert all(bool(s["sql"]) and s["method"] == "sqlite_readonly" for s in sql_steps)

    # 2. "How does PulseOps detect anomalies?" -> portfolio-wide, NOT Operations-only
    s2, _, _, b2 = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "How does PulseOps detect anomalies?", "provider": "local", "api_key": ""},
    )
    assert s2 == 200
    anom_step = next(s for s in b2["tool_trace"] if s["tool"] == "detect_anomalies")
    assert anom_step["input"]["department"] is None
    assert "INV-2025-0742" in b2["answer_markdown"]
    assert "INV-2025-1019" in b2["answer_markdown"]

    # 3. "Salesforce" must not hijack to Sales department
    s3, _, _, b3 = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "Audit Salesforce spend anomalies", "provider": "local", "api_key": ""},
    )
    assert s3 == 200
    sf_step = next(s for s in b3["tool_trace"] if s["tool"] == "detect_anomalies")
    assert sf_step["input"]["department"] is None

    # 4. Multi-department comparison retains both departments
    s4, _, _, b4 = http_request(
        base_url,
        "POST",
        "/api/copilot/ask",
        {"question": "Compare Engineering and Marketing budget variance", "provider": "local", "api_key": ""},
    )
    assert s4 == 200
    cmp_step = next(s for s in b4["tool_trace"] if s["tool"] == "compute_department_variance")
    assert cmp_step["input"]["department"] == ["Engineering", "Marketing"]
    assert "Engineering" in b4["answer_markdown"] and "Marketing" in b4["answer_markdown"]


@pytest.mark.parametrize(
    "workflow_id,bad_params",
    [
        ("anomaly_watchdog", {"z_threshold": "nan"}),
        ("anomaly_watchdog", {"z_threshold": "inf"}),
        ("anomaly_watchdog", {"z_threshold": "-inf"}),
        ("anomaly_watchdog", {"z_threshold": True}),
        ("anomaly_watchdog", {"z_threshold": False}),
        ("monthly_close", {"variance_threshold_pct": "NaN"}),
        ("monthly_close", {"variance_threshold_pct": True}),
        ("exec_brief", {"z_threshold": False}),
    ],
)
def test_workflow_rejects_nan_inf_and_bool_params(live_server, workflow_id, bad_params):
    """Regression: workflow float parser must reject NaN, Inf, and booleans with HTTP 400."""
    base_url, _ = live_server
    status, _, _, body = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": workflow_id, "params": bad_params},
    )
    assert status == 400
    assert body["ok"] is False


def test_exec_brief_no_double_counting_and_monthly_close_dynamic_over_depts(live_server):
    """Regression: exec_brief avoids double-counting price_hike_and_inactive subs & surfaces spikes;
    monthly_close dynamically interpolates over-budget departments instead of hardcoding names.
    """
    from backend.analytics import check_subscriptions

    base_url, db_path = live_server

    # Insert a subscription that is BOTH inactive (>90d) AND price-hiked (>=15%)
    _exec_sqlite(
        db_path,
        """
        INSERT INTO subscriptions (
            vendor, department, category, seats, monthly_cost,
            prev_monthly_cost, last_used_days, renewal_date, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("LegacyBI Pro", "Operations", "Analytics", 25, 2500.0, 1500.0, 125, "2026-03-01", "active"),
    )

    sub_audit = check_subscriptions(db_path)
    dual = [s for s in sub_audit["subscriptions"] if s["flag"] == "price_hike_and_inactive"]
    assert len(dual) == 1

    s_eb, _, _, b_eb = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": "exec_brief", "params": {"variance_threshold_pct": 5.0, "z_threshold": 2.0}},
    )
    assert s_eb == 200
    run_eb = b_eb["run"]
    md_eb = run_eb["output_markdown"]

    # Extract Lever #1 (inactive) and Lever #2 (active hikes) annual savings from Markdown
    m1 = re.search(r"Terminate >90-Day Inactive SaaS:\*\* Reclaim \$([0-9,]+\.\d{2})/yr", md_eb)
    m2 = re.search(r"Renegotiate Active Vendor Price Hikes:\*\* Address \$([0-9,]+\.\d{2})/yr", md_eb)
    assert m1 and m2, f"Expected savings levers not found in exec_brief markdown:\n{md_eb}"
    lever1 = float(m1.group(1).replace(",", ""))
    lever2 = float(m2.group(1).replace(",", ""))
    assert (lever1 + lever2) == pytest.approx(sub_audit["annual_savings_opp"], abs=0.01)

    # Confirm z_threshold spikes are surfaced in exec_brief markdown and webhook metrics
    assert "Spend Spike Watchlist (Z >= 2.0)" in md_eb
    assert "Spend Spikes" in run_eb["webhook_payload"]["metrics"]

    # Confirm monthly_close does not hardcode department names when threshold exceeds all dept variances
    s_mc, _, _, b_mc = http_request(
        base_url,
        "POST",
        "/api/workflows/run",
        {"workflow_id": "monthly_close", "params": {"variance_threshold_pct": 40.0, "z_threshold": 2.0}},
    )
    assert s_mc == 200
    mc_md = b_mc["run"]["output_markdown"]
    assert "All departments are tracking within the 40.0% variance threshold" in mc_md
    assert "VP Engineering & Marketing" not in mc_md


def test_evals_still_pass_12_of_12_after_sample_csv_upload(live_server):
    """Confirm full 12/12 eval harness passes even after ingesting the Q4 sample CSV."""
    base_url, _ = live_server
    _, _, sample_raw, _ = http_request(base_url, "GET", "/api/sample-csv")
    up_status, _, _, _ = http_request(
        base_url,
        "POST",
        "/api/data/upload",
        {"filename": "q4_upload_sample.csv", "csv_text": sample_raw.decode("utf-8")},
    )
    assert up_status == 200

    ev_status, _, _, ev_body = http_request(base_url, "POST", "/api/evals/run", {})
    assert ev_status == 200
    assert ev_body["total"] == 12
    assert ev_body["passed"] == 12
    assert ev_body["pass_rate"] == pytest.approx(100.0, abs=1e-6)
