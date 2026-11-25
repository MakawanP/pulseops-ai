"""Pure deterministic financial analytics, variance/EAC math, anomaly detection, and SaaS watchdog."""

from __future__ import annotations

import math
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from backend.database import DEPARTMENTS, get_connection, open_readonly_snapshot


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Compute linear-interpolated percentile on a pre-sorted list of floats."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * pct
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return sorted_vals[low]
    weight = pos - low
    return sorted_vals[low] * (1.0 - weight) + sorted_vals[high] * weight


def compute_monthly_trend(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Compute monthly budget vs. actual spend and variance across all fiscal periods."""
    ctx = nullcontext(conn) if conn is not None else open_readonly_snapshot(db_path)
    with ctx as db:
        budget_rows = db.execute(
            """
            SELECT month, ROUND(SUM(budget_amount), 2) AS budget
            FROM budgets
            GROUP BY month
            ORDER BY month ASC
            """
        ).fetchall()

        actual_rows = db.execute(
            """
            SELECT month, ROUND(SUM(amount), 2) AS actual
            FROM transactions
            GROUP BY month
            ORDER BY month ASC
            """
        ).fetchall()

    budgets_by_month = {r["month"]: float(r["budget"] or 0.0) for r in budget_rows}
    actuals_by_month = {r["month"]: float(r["actual"] or 0.0) for r in actual_rows}
    all_months = sorted(set(budgets_by_month.keys()) | set(actuals_by_month.keys()))

    trend: list[dict[str, Any]] = []
    for month in all_months:
        budget = round(budgets_by_month.get(month, 0.0), 2)
        actual = round(actuals_by_month.get(month, 0.0), 2)
        variance = round(actual - budget, 2)
        trend.append(
            {
                "month": month,
                "budget": budget,
                "actual": actual,
                "variance": variance,
            }
        )
    return trend


def compute_department_variance(
    db_path: str | Path | None = None,
    department: str | None = None,
    variance_threshold_pct: float = 5.0,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Compute per-department budget vs. actuals, variance %, period-scoped EAC, and status."""
    ctx = nullcontext(conn) if conn is not None else open_readonly_snapshot(db_path)
    with ctx as db:
        budget_rows = db.execute(
            """
            SELECT department, month, ROUND(SUM(budget_amount), 2) AS month_budget
            FROM budgets
            GROUP BY department, month
            ORDER BY department, month ASC
            """
        ).fetchall()

        monthly_actual_rows = db.execute(
            """
            SELECT department, month, ROUND(SUM(amount), 2) AS month_actual
            FROM transactions
            GROUP BY department, month
            ORDER BY department, month ASC
            """
        ).fetchall()

    budgets_total: dict[str, float] = {}
    budget_months_by_dept: dict[str, list[str]] = {}
    global_budget_months_set: set[str] = set()
    for row in budget_rows:
        dept_name = str(row["department"])
        m_str = str(row["month"])
        budgets_total[dept_name] = round(
            budgets_total.get(dept_name, 0.0) + float(row["month_budget"] or 0.0), 2
        )
        budget_months_by_dept.setdefault(dept_name, []).append(m_str)
        global_budget_months_set.add(m_str)
    global_fy_months = sorted(global_budget_months_set)

    dept_monthly: dict[str, list[tuple[str, float]]] = {}
    for row in monthly_actual_rows:
        dept_monthly.setdefault(str(row["department"]), []).append(
            (str(row["month"]), float(row["month_actual"] or 0.0))
        )

    all_depts = list(DEPARTMENTS)
    for d in sorted(set(budgets_total.keys()) | set(dept_monthly.keys())):
        if d not in all_depts:
            all_depts.append(d)

    if department:
        target = department.strip().lower()
        all_depts = [d for d in all_depts if d.lower() == target]

    results: list[dict[str, Any]] = []
    for dept in all_depts:
        budget = round(budgets_total.get(dept, 0.0), 2)
        fy_months = sorted(budget_months_by_dept.get(dept, [])) or global_fy_months
        fy_months_set = set(fy_months)
        total_months = len(fy_months) or 12

        month_series = dept_monthly.get(dept, [])
        tx_by_month = dict(month_series)
        actual = round(sum(val for _, val in month_series), 2)
        variance = round(actual - budget, 2)
        unbudgeted = bool(budget == 0.0 and actual > 0.0)
        variance_pct = round((variance / budget) * 100.0, 2) if budget > 0 else 0.0

        # Deterministic fiscal-period-scoped EAC (Estimate at Completion):
        # - Scope trailing run-rate strictly to the active fiscal budget calendar (elapsed calendar
        #   months, counting $0 gap months as elapsed rather than remaining future months).
        # - Floor completed-FY EAC at actual incurred spend so out-of-period postings (e.g., next FY)
        #   can never drop EAC below already-incurred actuals.
        posted_fy_indices = [idx for idx, m in enumerate(fy_months) if m in tx_by_month]
        non_fy_actual = round(
            sum(val for m, val in month_series if m not in fy_months_set), 2
        )

        if not posted_fy_indices:
            if non_fy_actual > 0.0:
                eac = round(max(budget + non_fy_actual, actual), 2)
            else:
                eac = round(budget, 2)
        else:
            elapsed_months = posted_fy_indices[-1] + 1
            elapsed_series = [tx_by_month.get(m, 0.0) for m in fy_months[:elapsed_months]]
            fy_actual = round(sum(elapsed_series), 2)

            if elapsed_months < total_months:
                recent_slice = elapsed_series[-min(3, elapsed_months) :]
                recent_avg = sum(recent_slice) / len(recent_slice)
                fy_eac = round(fy_actual + (total_months - elapsed_months) * recent_avg, 2)
            else:
                recent_slice = elapsed_series[-min(3, total_months) :]
                recent_annualized = (sum(recent_slice) / len(recent_slice)) * total_months
                fy_eac = round(max(0.6 * fy_actual + 0.4 * recent_annualized, fy_actual), 2)

            eac = round(max(fy_eac + non_fy_actual, actual), 2)

        if unbudgeted or variance_pct > variance_threshold_pct:
            status = "over_budget"
        elif variance_pct > 0.0:
            status = "watch"
        else:
            status = "on_track"

        results.append(
            {
                "department": dept,
                "budget": budget,
                "actual": actual,
                "variance": variance,
                "variance_pct": variance_pct,
                "eac": eac,
                "status": status,
                "unbudgeted": unbudgeted,
            }
        )

    results.sort(
        key=lambda item: (1 if item["unbudgeted"] else 0, item["variance_pct"], item["variance"]),
        reverse=True,
    )
    return results


def detect_duplicate_invoices(
    db_path: str | Path | None = None,
    department: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Audit transactions ledger for case-insensitive duplicate invoice_id postings."""
    ctx = nullcontext(conn) if conn is not None else get_connection(db_path, read_only=True)
    db = ctx.__enter__()
    try:
        sql = """
            SELECT
                MIN(TRIM(invoice_id)) AS invoice_id,
                UPPER(TRIM(invoice_id)) AS norm_invoice_id,
                MIN(department) AS department,
                MIN(vendor) AS vendor,
                COUNT(DISTINCT department) AS dept_count,
                COUNT(DISTINCT vendor) AS vendor_count,
                COUNT(*) AS dup_count,
                ROUND(MAX(amount), 2) AS unit_amount,
                ROUND(SUM(amount) - MIN(amount), 2) AS duplicate_exposure,
                MIN(tx_date) AS first_date,
                MAX(tx_date) AS last_date
            FROM transactions
        """
        params: list[Any] = []
        if department:
            sql += " WHERE LOWER(department) = LOWER(?)"
            params.append(department.strip())
        sql += """
            GROUP BY UPPER(TRIM(invoice_id))
            HAVING COUNT(*) > 1
            ORDER BY duplicate_exposure DESC, norm_invoice_id ASC
        """
        rows = db.execute(sql, params).fetchall()
    finally:
        if conn is None:
            db.close()

    findings: list[dict[str, Any]] = []
    for row in rows:
        inv_id = str(row["invoice_id"])
        dup_count = int(row["dup_count"])
        exposure = round(float(row["duplicate_exposure"]), 2)
        unit_amt = round(float(row["unit_amount"]), 2)
        cross_note = ""
        if int(row["dept_count"]) > 1 or int(row["vendor_count"]) > 1:
            cross_note = " across multiple departments/vendors"
        findings.append(
            {
                "id": f"dup-{inv_id}",
                "type": "duplicate_invoice",
                "severity": "high",
                "department": str(row["department"]),
                "vendor": str(row["vendor"]),
                "amount": exposure,
                "detail": (
                    f"Invoice {inv_id} posted {dup_count}x{cross_note} "
                    f"({row['first_date']} & {row['last_date']}) "
                    f"at ${unit_amt:,.2f} each — ${exposure:,.2f} duplicate exposure."
                ),
                "tx_date": str(row["last_date"]),
                "invoice_id": inv_id,
            }
        )
    return findings


def detect_spend_anomalies(
    db_path: str | Path | None = None,
    z_threshold: float = 2.0,
    department: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Detect statistical transaction spend spikes per department using Z-score + IQR upper fence."""
    if not math.isfinite(z_threshold):
        raise ValueError("z_threshold must be a finite number.")

    db = conn if conn is not None else get_connection(db_path, read_only=True)
    try:
        sql = """
            SELECT id, tx_date, department, vendor, category, amount, invoice_id, description
            FROM transactions
        """
        params: list[Any] = []
        if department:
            sql += " WHERE LOWER(department) = LOWER(?)"
            params.append(department.strip())
        sql += " ORDER BY department, amount DESC"
        rows = db.execute(sql, params).fetchall()
    finally:
        if conn is None:
            db.close()

    by_dept: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        item = dict(r)
        by_dept.setdefault(str(item["department"]), []).append(item)

    anomalies: list[dict[str, Any]] = []
    for dept, items in by_dept.items():
        if len(items) < 4:
            continue
        amounts = [float(x["amount"]) for x in items if math.isfinite(float(x["amount"]))]
        if len(amounts) < 4:
            continue
        mean = sum(amounts) / len(amounts)
        # Scale by max magnitude before squaring so extreme floats can never OverflowError
        max_dev = max(abs(x - mean) for x in amounts)
        if max_dev <= 0.0:
            continue
        scaled_var = sum(((x - mean) / max_dev) ** 2 for x in amounts) / len(amounts)
        std = max_dev * math.sqrt(scaled_var)
        if std <= 0.0 or not math.isfinite(std):
            continue

        sorted_amts = sorted(amounts)
        q1 = _percentile(sorted_amts, 0.25)
        q3 = _percentile(sorted_amts, 0.75)
        iqr = q3 - q1
        upper_fence = q3 + 1.5 * iqr

        for tx in items:
            amt = float(tx["amount"])
            if not math.isfinite(amt):
                continue
            z_score = (amt - mean) / std
            if z_score >= z_threshold and amt > upper_fence:
                severity = "high" if (z_score >= 2.8 and amt >= 30000.0) or z_score >= 3.8 else "medium"
                pct_above = ((amt - mean) / mean) * 100.0 if mean > 0 else 0.0
                anomalies.append(
                    {
                        "id": f"spike-{tx['id']}",
                        "type": "spend_spike",
                        "severity": severity,
                        "department": dept,
                        "vendor": str(tx["vendor"]),
                        "amount": round(amt, 2),
                        "detail": (
                            f"Z-score {z_score:.2f} (+{pct_above:.0f}% vs {dept} avg ${mean:,.0f}; "
                            f"IQR fence ${upper_fence:,.0f}) — {tx['description'] or tx['category']}"
                        ),
                        "tx_date": str(tx["tx_date"]),
                        "invoice_id": str(tx["invoice_id"]),
                    }
                )

    anomalies.sort(key=lambda x: (0 if x["severity"] == "high" else 1, -x["amount"]))
    return anomalies


def detect_all_anomalies(
    db_path: str | Path | None = None,
    z_threshold: float = 2.0,
    department: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Combine duplicate invoice audit findings and statistical spend spikes from a single snapshot."""
    ctx = nullcontext(conn) if conn is not None else open_readonly_snapshot(db_path)
    with ctx as db:
        duplicates = detect_duplicate_invoices(db_path, department=department, conn=db)
        spikes = detect_spend_anomalies(db_path, z_threshold=z_threshold, department=department, conn=db)
    combined = duplicates + spikes
    combined.sort(key=lambda x: (0 if x["severity"] == "high" else 1, -x["amount"], x["invoice_id"]))
    return combined


def check_subscriptions(
    db_path: str | Path | None = None,
    hike_threshold_pct: float = 15.0,
    inactive_days_threshold: int = 90,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Audit SaaS subscriptions for price hikes (>=15%) and >90-day inactivity."""
    db = conn if conn is not None else get_connection(db_path, read_only=True)
    try:
        rows = db.execute(
            """
            SELECT id, vendor, department, category, seats, monthly_cost,
                   prev_monthly_cost, last_used_days, renewal_date, status
            FROM subscriptions
            ORDER BY monthly_cost DESC
            """
        ).fetchall()
    finally:
        if conn is None:
            db.close()

    subs_out: list[dict[str, Any]] = []
    price_hikes: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    monthly_savings = 0.0

    for row in rows:
        monthly = round(float(row["monthly_cost"]), 2)
        prev = round(float(row["prev_monthly_cost"]), 2)
        change_pct = round(((monthly - prev) / prev) * 100.0, 2) if prev > 0 else 0.0
        last_used = int(row["last_used_days"])

        is_inactive = last_used > inactive_days_threshold
        is_hike = change_pct >= hike_threshold_pct

        if is_inactive and is_hike:
            flag: str | None = "price_hike_and_inactive"
            monthly_savings += monthly
        elif is_inactive:
            flag = "inactive_90d"
            monthly_savings += monthly
        elif is_hike:
            flag = "price_hike"
            monthly_savings += max(0.0, monthly - prev)
        else:
            flag = None

        item = {
            "id": int(row["id"]),
            "vendor": str(row["vendor"]),
            "department": str(row["department"]),
            "monthly_cost": monthly,
            "prev_monthly_cost": prev,
            "price_change_pct": change_pct,
            "last_used_days": last_used,
            "flag": flag,
            "status": str(row["status"]),
        }
        subs_out.append(item)
        if is_hike:
            price_hikes.append(item)
        if is_inactive:
            inactive.append(item)

    subs_out.sort(key=lambda x: (0 if x["flag"] else 1, -x["monthly_cost"]))
    monthly_savings = round(monthly_savings, 2)
    annual_savings = round(monthly_savings * 12.0, 2)

    return {
        "subscriptions": subs_out,
        "flagged_count": sum(1 for s in subs_out if s["flag"] is not None),
        "price_hikes": price_hikes,
        "inactive": inactive,
        "monthly_savings_opp": monthly_savings,
        "annual_savings_opp": annual_savings,
    }


def get_last_eval_score(
    db_path: str | Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> float | None:
    """Fetch the latest stored eval pass_rate from eval_runs, or None if no eval has run."""
    db = conn if conn is not None else get_connection(db_path, read_only=True)
    try:
        row = db.execute(
            "SELECT pass_rate FROM eval_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return round(float(row["pass_rate"]), 2) if row else None
    finally:
        if conn is None:
            db.close()


def get_overview(
    db_path: str | Path | None = None,
    z_threshold: float = 2.0,
    variance_threshold_pct: float = 5.0,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Assemble the complete GET /api/overview payload from a single consistent read snapshot."""
    ctx = nullcontext(conn) if conn is not None else open_readonly_snapshot(db_path)
    with ctx as db:
        monthly_trend = compute_monthly_trend(db_path, conn=db)
        dept_variance = compute_department_variance(
            db_path, variance_threshold_pct=variance_threshold_pct, conn=db
        )
        duplicates = detect_duplicate_invoices(db_path, conn=db)
        spikes = detect_spend_anomalies(db_path, z_threshold=z_threshold, conn=db)
        sub_audit = check_subscriptions(db_path, conn=db)
        last_eval_score = get_last_eval_score(db_path, conn=db)

    anomalies = duplicates + spikes
    anomalies.sort(key=lambda x: (0 if x["severity"] == "high" else 1, -x["amount"], x["invoice_id"]))

    total_budget = round(sum(d["budget"] for d in dept_variance), 2)
    total_actual = round(sum(d["actual"] for d in dept_variance), 2)
    net_variance = round(total_actual - total_budget, 2)
    variance_pct = round((net_variance / total_budget) * 100.0, 2) if total_budget > 0 else 0.0
    total_eac = round(sum(d["eac"] for d in dept_variance), 2)

    return {
        "kpis": {
            "total_budget": total_budget,
            "total_actual": total_actual,
            "net_variance": net_variance,
            "variance_pct": variance_pct,
            "total_eac": total_eac,
            "anomaly_count": len(anomalies),
            "duplicate_count": len(duplicates),
            "subscription_savings_opp": sub_audit["annual_savings_opp"],
            "last_eval_score": last_eval_score,
        },
        "monthly_trend": monthly_trend,
        "department_variance": dept_variance,
        "anomalies": anomalies,
        "subscriptions": sub_audit["subscriptions"],
    }
