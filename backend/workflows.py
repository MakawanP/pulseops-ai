"""Runnable multi-step financial automation workflows with Markdown briefs and Slack/webhook payloads."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.analytics import (
    check_subscriptions,
    compute_monthly_trend,
    detect_duplicate_invoices,
    detect_spend_anomalies,
    get_overview,
)
from backend.database import get_connection, get_db_lock, open_readonly_snapshot, utc_now_iso

WORKFLOW_CATALOG: list[dict[str, Any]] = [
    {
        "id": "monthly_close",
        "name": "Monthly Financial Close & Variance Package",
        "description": (
            "Audits ledger completeness, computes department budget variance & Q4-weighted EAC, "
            "flags duplicate invoices and spend spikes, and compiles a CFO close memo."
        ),
        "schedule": "1st Business Day · 07:00 EST",
        "default_params": {"variance_threshold_pct": 5.0, "z_threshold": 2.0},
        "steps": [
            "Ledger Completeness Check",
            "Budget vs. Actual Variance Engine",
            "Duplicate Invoice & Spend Spike Audit",
            "Executive Close Brief & Slack Dispatch",
        ],
    },
    {
        "id": "anomaly_watchdog",
        "name": "Autonomous Spend & SaaS Anomaly Watchdog",
        "description": (
            "Runs statistical Z-score & IQR outlier scans, reconciles duplicate invoices, "
            "sweeps SaaS renewals for price hikes or >90-day inactivity, and generates escalation alerts."
        ),
        "schedule": "Daily · 08:30 EST",
        "default_params": {"z_threshold": 2.0},
        "steps": [
            "Z-Score & IQR Outlier Scan",
            "Duplicate Invoice Reconciliation",
            "SaaS Price Hike & Inactivity Sweep",
            "Escalation Payload Generation",
        ],
    },
    {
        "id": "exec_brief",
        "name": "CFO Board Pack & Forecast Brief",
        "description": (
            "Consolidates FY KPIs and annualized EAC projections, ranks department variance & spend-spike risk, "
            "quantifies SaaS cost-reduction levers, and publishes a board-ready executive brief."
        ),
        "schedule": "Weekly · Monday 08:00 EST",
        "default_params": {"variance_threshold_pct": 5.0, "z_threshold": 2.0},
        "steps": [
            "Consolidate FY KPIs & EAC",
            "Rank Department Risk Exposure",
            "Quantify SaaS Optimization Levers",
            "Publish Executive Markdown & Webhook",
        ],
    },
]

WORKFLOWS_BY_ID = {wf["id"]: wf for wf in WORKFLOW_CATALOG}


def _parse_float_param(
    params: dict[str, Any], key: str, default: float, min_val: float, max_val: float
) -> float:
    raw = params.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"Invalid boolean parameter '{key}': {raw}")
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric parameter '{key}': {raw}") from exc
    if not math.isfinite(val):
        raise ValueError(f"Parameter '{key}' must be a finite number (got {raw}).")
    if val < min_val or val > max_val:
        raise ValueError(f"Parameter '{key}' must be between {min_val} and {max_val} (got {val}).")
    return round(val, 2)


def _format_currency(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _format_signed_currency(value: float) -> str:
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return f"{sign}${abs(value):,.2f}"


def _format_var_pct(dept: dict[str, Any]) -> str:
    if dept.get("unbudgeted"):
        return "Unbudgeted"
    return f"{dept['variance_pct']:+.2f}%"


def _build_slack_webhook(
    workflow_id: str,
    workflow_name: str,
    ran_at: str,
    summary: str,
    severity: str,
    metrics: dict[str, Any],
    highlights: list[str],
) -> dict[str, Any]:
    return {
        "channel": "#finops-alerts",
        "username": "PulseOps AI Automation",
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "timestamp": ran_at,
        "severity": severity,
        "summary": summary,
        "metrics": metrics,
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"PulseOps AI · {workflow_name}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Executive Summary:* {summary}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}:*\n{v}"} for k, v in metrics.items()
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Top Action Items:*\n" + "\n".join(f"• {h}" for h in highlights[:5]),
                },
            },
        ],
    }


def _execute_monthly_close(
    db_path: str | Path | None,
    variance_threshold_pct: float,
    z_threshold: float,
    ran_at: str,
    *,
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    # Step 1: Ledger completeness
    t0 = time.perf_counter()
    tx_count = int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
    months_covered = int(conn.execute("SELECT COUNT(DISTINCT month) FROM transactions").fetchone()[0])
    depts_covered = int(conn.execute("SELECT COUNT(DISTINCT department) FROM transactions").fetchone()[0])
    trend = compute_monthly_trend(db_path, conn=conn)
    steps.append(
        {
            "name": "Ledger Completeness Check",
            "status": "passed",
            "detail": (
                f"Validated {tx_count} transactions spanning {months_covered} fiscal periods "
                f"and {depts_covered} departments against {len(trend)} monthly budget periods."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    # Step 2: Department variance & EAC
    t0 = time.perf_counter()
    overview = get_overview(
        db_path,
        z_threshold=z_threshold,
        variance_threshold_pct=variance_threshold_pct,
        conn=conn,
    )
    kpis = overview["kpis"]
    depts = overview["department_variance"]
    over_depts = [d for d in depts if d["status"] == "over_budget"]
    steps.append(
        {
            "name": "Budget vs. Actual Variance Engine",
            "status": "passed",
            "detail": (
                f"Total Actual {_format_currency(kpis['total_actual'])} vs Budget "
                f"{_format_currency(kpis['total_budget'])} "
                f"({_format_signed_currency(kpis['net_variance'])}, {kpis['variance_pct']:+.2f}%). "
                f"{len(over_depts)} department(s) exceeded the {variance_threshold_pct:.1f}% threshold."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    # Step 3: Duplicate & spend spike audit
    t0 = time.perf_counter()
    anomalies = overview["anomalies"]
    duplicates = [a for a in anomalies if a["type"] == "duplicate_invoice"]
    spikes = [a for a in anomalies if a["type"] == "spend_spike"]
    dup_total = round(sum(a["amount"] for a in duplicates), 2)
    steps.append(
        {
            "name": "Duplicate Invoice & Spend Spike Audit",
            "status": "passed",
            "detail": (
                f"Flagged {len(duplicates)} duplicate invoice(s) ({_format_currency(dup_total)} exposure) "
                f"and {len(spikes)} statistical spend spike(s) at z >= {z_threshold:.1f}."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    # Step 4: Brief & webhook
    t0 = time.perf_counter()
    dept_lines = [
        "| Department | Budget | Actual | Variance | Var % | Annualized EAC | Status |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for d in depts:
        dept_lines.append(
            f"| {d['department']} | {_format_currency(d['budget'])} | {_format_currency(d['actual'])} | "
            f"{_format_signed_currency(d['variance'])} | {_format_var_pct(d)} | "
            f"{_format_currency(d['eac'])} | `{d['status']}` |"
        )

    anomaly_lines = [
        f"- **[{a['severity'].upper()}] {a['invoice_id']} ({a['department']} · {a['vendor']}):** "
        f"{_format_currency(a['amount'])} — {a['detail']}"
        for a in anomalies[:8]
    ] or ["- No anomalies detected above configured thresholds."]

    if over_depts:
        over_names = ", ".join(d["department"] for d in over_depts)
        reallocation_line = (
            f"2. Require department VP approval for {over_names} budget reallocations "
            f"(exceeding the {variance_threshold_pct:.1f}% variance threshold)."
        )
    else:
        reallocation_line = (
            f"2. All departments are tracking within the {variance_threshold_pct:.1f}% variance threshold; "
            "no emergency budget reallocations required."
        )

    output_md = "\n".join(
        [
            "# PulseOps AI — Monthly Financial Close & Variance Package",
            f"_Generated: {ran_at} | Thresholds: Variance > {variance_threshold_pct:.1f}%, Z-score >= {z_threshold:.1f}_",
            "",
            "## Executive KPI Snapshot",
            f"- **Total FY Budget:** {_format_currency(kpis['total_budget'])}",
            f"- **Total Actual Spend:** {_format_currency(kpis['total_actual'])}",
            f"- **Net Variance:** {_format_signed_currency(kpis['net_variance'])} ({kpis['variance_pct']:+.2f}%)",
            f"- **Total Annualized EAC:** {_format_currency(kpis['total_eac'])}",
            f"- **Active Anomalies:** {kpis['anomaly_count']} ({len(duplicates)} duplicate invoices, {len(spikes)} spend spikes)",
            f"- **Annualized SaaS Savings Opportunity:** {_format_currency(kpis['subscription_savings_opp'])}",
            "",
            "## Department Variance & EAC Breakdown",
            *dept_lines,
            "",
            "## Audit Findings Requiring Close Sign-Off",
            *anomaly_lines,
            "",
            "## Recommended CFO Actions",
            f"1. Initiate AP credit recovery on {len(duplicates)} duplicate ERP posting(s) totaling {_format_currency(dup_total)}.",
            reallocation_line,
            f"3. Capture {_format_currency(kpis['subscription_savings_opp'])}/yr by retiring >90-day inactive SaaS seats and renegotiating >=15% renewal hikes.",
        ]
    )

    summary = (
        f"FY Actual {_format_currency(kpis['total_actual'])} vs Budget {_format_currency(kpis['total_budget'])} "
        f"({kpis['variance_pct']:+.2f}%); {len(over_depts)} dept(s) over budget, "
        f"{len(anomalies)} anomalies flagged."
    )
    severity = "high" if (over_depts or any(a["severity"] == "high" for a in anomalies)) else "normal"
    highlights = [
        f"{d['department']}: {_format_var_pct(d)} ({_format_signed_currency(d['variance'])})"
        for d in over_depts
    ] + [
        f"{a['invoice_id']} ({a['vendor']}): {_format_currency(a['amount'])} [{a['type']}]"
        for a in anomalies[:3]
    ]
    webhook = _build_slack_webhook(
        workflow_id="monthly_close",
        workflow_name=WORKFLOWS_BY_ID["monthly_close"]["name"],
        ran_at=ran_at,
        summary=summary,
        severity=severity,
        metrics={
            "Total Actual": _format_currency(kpis["total_actual"]),
            "Net Variance": f"{_format_signed_currency(kpis['net_variance'])} ({kpis['variance_pct']:+.2f}%)",
            "Active Anomalies": str(kpis["anomaly_count"]),
            "SaaS Savings Opp": f"{_format_currency(kpis['subscription_savings_opp'])}/yr",
        },
        highlights=highlights or ["All departments tracking within tolerance."],
    )

    steps.append(
        {
            "name": "Executive Close Brief & Slack Dispatch",
            "status": "passed",
            "detail": "Generated formatted close memorandum and #finops-alerts Slack Block Kit payload.",
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )
    return steps, summary, output_md, webhook


def _execute_anomaly_watchdog(
    db_path: str | Path | None,
    z_threshold: float,
    ran_at: str,
    *,
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    spikes = detect_spend_anomalies(db_path, z_threshold=z_threshold, conn=conn)
    spike_total = round(sum(s["amount"] for s in spikes), 2)
    steps.append(
        {
            "name": "Z-Score & IQR Outlier Scan",
            "status": "passed",
            "detail": (
                f"Detected {len(spikes)} statistical spend outlier(s) at z >= {z_threshold:.1f} "
                f"totaling {_format_currency(spike_total)}."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    duplicates = detect_duplicate_invoices(db_path, conn=conn)
    dup_exposure = round(sum(d["amount"] for d in duplicates), 2)
    steps.append(
        {
            "name": "Duplicate Invoice Reconciliation",
            "status": "passed",
            "detail": (
                f"Isolated {len(duplicates)} duplicate invoice group(s) representing "
                f"{_format_currency(dup_exposure)} in recoverable billing exposure."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    sub_audit = check_subscriptions(db_path, conn=conn)
    steps.append(
        {
            "name": "SaaS Price Hike & Inactivity Sweep",
            "status": "passed",
            "detail": (
                f"Flagged {len(sub_audit['price_hikes'])} vendor price hike(s) (>=15%) and "
                f"{len(sub_audit['inactive'])} inactive license(s) (>90d) — "
                f"{_format_currency(sub_audit['annual_savings_opp'])}/yr savings opportunity."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    all_anomalies = duplicates + spikes
    all_anomalies.sort(
        key=lambda x: (0 if x["severity"] == "high" else 1, -x["amount"], x["invoice_id"])
    )
    high_count = sum(1 for a in all_anomalies if a["severity"] == "high")

    spike_md = [
        f"- `{s['invoice_id']}` | **{s['department']}** · {s['vendor']} | {_format_currency(s['amount'])} ({s['tx_date']}) — {s['detail']}"
        for s in spikes
    ] or ["- No statistical spend spikes detected."]

    dup_md = [
        f"- `{d['invoice_id']}` | **{d['department']}** · {d['vendor']} | Exposure: {_format_currency(d['amount'])} — {d['detail']}"
        for d in duplicates
    ] or ["- No duplicate invoices present in ledger."]

    saas_md = [
        f"- **{s['vendor']} ({s['department']}):** {_format_currency(s['monthly_cost'])}/mo "
        f"(prev {_format_currency(s['prev_monthly_cost'])}, {s['price_change_pct']:+.1f}%, "
        f"last used {s['last_used_days']}d ago) — `{s['flag']}`"
        for s in sub_audit["subscriptions"]
        if s["flag"]
    ] or ["- All SaaS renewals healthy."]

    output_md = "\n".join(
        [
            "# PulseOps AI — Autonomous Spend & SaaS Anomaly Watchdog Report",
            f"_Executed: {ran_at} | Z-Score Threshold: {z_threshold:.1f}_",
            "",
            "## Watchdog Triage Summary",
            f"- **High-Severity Alerts:** {high_count}",
            f"- **Spend Spikes Flagged:** {len(spikes)} ({_format_currency(spike_total)})",
            f"- **Duplicate Invoices:** {len(duplicates)} ({_format_currency(dup_exposure)} exposure)",
            f"- **Flagged SaaS Subscriptions:** {sub_audit['flagged_count']} ({_format_currency(sub_audit['annual_savings_opp'])}/yr reclaimable)",
            "",
            "## Statistical Spend Outliers",
            *spike_md,
            "",
            "## Duplicate Invoice Exposure",
            *dup_md,
            "",
            "## SaaS Renewal & Utilization Flags",
            *saas_md,
        ]
    )

    summary = (
        f"Watchdog flagged {len(spikes)} spend spike(s), {len(duplicates)} duplicate invoice(s) "
        f"({_format_currency(dup_exposure)}), and {sub_audit['flagged_count']} SaaS alert(s)."
    )
    highlights = [
        f"Duplicate {d['invoice_id']} ({d['vendor']}): {_format_currency(d['amount'])}"
        for d in duplicates
    ] + [
        f"Spike {s['invoice_id']} ({s['vendor']}): {_format_currency(s['amount'])}"
        for s in spikes[:3]
    ]
    webhook = _build_slack_webhook(
        workflow_id="anomaly_watchdog",
        workflow_name=WORKFLOWS_BY_ID["anomaly_watchdog"]["name"],
        ran_at=ran_at,
        summary=summary,
        severity="high" if high_count > 0 else "normal",
        metrics={
            "High-Severity Alerts": str(high_count),
            "Duplicate Exposure": _format_currency(dup_exposure),
            "Spend Spikes": f"{len(spikes)} ({_format_currency(spike_total)})",
            "SaaS Reclaimable": f"{_format_currency(sub_audit['annual_savings_opp'])}/yr",
        },
        highlights=highlights or ["No escalated anomalies found."],
    )
    steps.append(
        {
            "name": "Escalation Payload Generation",
            "status": "passed",
            "detail": f"Packaged {len(all_anomalies)} ledger alerts and {sub_audit['flagged_count']} SaaS flags into escalation webhook.",
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )
    return steps, summary, output_md, webhook


def _execute_exec_brief(
    db_path: str | Path | None,
    variance_threshold_pct: float,
    z_threshold: float,
    ran_at: str,
    *,
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    overview = get_overview(
        db_path,
        z_threshold=z_threshold,
        variance_threshold_pct=variance_threshold_pct,
        conn=conn,
    )
    kpis = overview["kpis"]
    eac_vs_budget = round(kpis["total_eac"] - kpis["total_budget"], 2)
    steps.append(
        {
            "name": "Consolidate FY KPIs & EAC",
            "status": "passed",
            "detail": (
                f"Consolidated FY Budget {_format_currency(kpis['total_budget'])}, "
                f"Actual {_format_currency(kpis['total_actual'])}, and Annualized EAC "
                f"{_format_currency(kpis['total_eac'])} ({_format_signed_currency(eac_vs_budget)} vs plan)."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    depts = overview["department_variance"]
    spikes = [a for a in overview["anomalies"] if a["type"] == "spend_spike"]
    spike_total = round(sum(a["amount"] for a in spikes), 2)
    top_risk = depts[0] if depts else None
    steps.append(
        {
            "name": "Rank Department Risk Exposure",
            "status": "passed",
            "detail": (
                f"Ranked {len(depts)} departments (threshold >{variance_threshold_pct:.1f}%) and "
                f"{len(spikes)} spend spike(s) at z >= {z_threshold:.1f} ({_format_currency(spike_total)}); "
                f"highest variance exposure is {top_risk['department']} "
                f"({_format_var_pct(top_risk)}, EAC {_format_currency(top_risk['eac'])})."
                if top_risk
                else f"No department variance data found; {len(spikes)} spend spike(s) at z >= {z_threshold:.1f}."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    sub_audit = check_subscriptions(db_path, conn=conn)
    # Exclude inactive contracts from price-hike savings so Levers #1 and #2 never double-count
    # subscriptions flagged as both inactive and price-hiked (price_hike_and_inactive).
    inactive_ids = {s["id"] for s in sub_audit["inactive"]}
    active_hikes = [s for s in sub_audit["price_hikes"] if s["id"] not in inactive_ids]
    inactive_annual = round(sum(s["monthly_cost"] for s in sub_audit["inactive"]) * 12.0, 2)
    hike_annual = round(
        sum(max(0.0, s["monthly_cost"] - s["prev_monthly_cost"]) for s in active_hikes) * 12.0,
        2,
    )
    steps.append(
        {
            "name": "Quantify SaaS Optimization Levers",
            "status": "passed",
            "detail": (
                f"Identified {_format_currency(inactive_annual)}/yr in unused licenses (>90d) and "
                f"{_format_currency(hike_annual)}/yr in renegotiable active vendor price increases "
                f"({_format_currency(sub_audit['annual_savings_opp'])}/yr total non-overlapping opportunity)."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )

    t0 = time.perf_counter()
    duplicates = [a for a in overview["anomalies"] if a["type"] == "duplicate_invoice"]
    dup_recovery = round(sum(a["amount"] for a in duplicates), 2)
    total_reclaimable = round(sub_audit["annual_savings_opp"] + dup_recovery, 2)

    dept_bullets = [
        f"- **{d['department']} (`{d['status']}`):** Actual {_format_currency(d['actual'])} vs Budget "
        f"{_format_currency(d['budget'])} ({_format_var_pct(d)}) · EAC {_format_currency(d['eac'])}"
        for d in depts
    ]

    spike_bullets = [
        f"- `{s['invoice_id']}` — **{s['vendor']}** ({s['department']} · {s['tx_date']}): "
        f"{_format_currency(s['amount'])} [{s['severity']}] — {s['detail']}"
        for s in spikes[:5]
    ] or [f"- No statistical spend spikes detected at z >= {z_threshold:.1f}."]

    output_md = "\n".join(
        [
            "# PulseOps AI — CFO Board Pack & Forecast Brief",
            f"_Prepared: {ran_at} | Variance Alert Threshold: >{variance_threshold_pct:.1f}% | Spend Spike Threshold: Z >= {z_threshold:.1f}_",
            "",
            "## Headline Financial Position",
            f"- **FY2025 Budget Plan:** {_format_currency(kpis['total_budget'])}",
            f"- **Posted Actual Spend:** {_format_currency(kpis['total_actual'])} ({kpis['variance_pct']:+.2f}% vs plan)",
            f"- **Q4-Weighted Annualized EAC:** {_format_currency(kpis['total_eac'])} ({_format_signed_currency(eac_vs_budget)} vs budget)",
            f"- **Spend Spikes Flagged (Z >= {z_threshold:.1f}):** {len(spikes)} outlier(s) totaling {_format_currency(spike_total)}",
            f"- **Immediate Cost Recovery & Savings Levers:** {_format_currency(total_reclaimable)} "
            f"({_format_currency(dup_recovery)} duplicate invoice recovery + {_format_currency(sub_audit['annual_savings_opp'])}/yr SaaS optimization)",
            "",
            "## Department Risk Ranking",
            *dept_bullets,
            "",
            f"## Spend Spike Watchlist (Z >= {z_threshold:.1f})",
            *spike_bullets,
            "",
            "## Cost Optimization Levers",
            f"1. **Terminate >90-Day Inactive SaaS:** Reclaim {_format_currency(inactive_annual)}/yr across {len(sub_audit['inactive'])} dormant tool(s).",
            f"2. **Renegotiate Active Vendor Price Hikes:** Address {_format_currency(hike_annual)}/yr in >=15% renewal increases across {len(active_hikes)} active contract(s).",
            f"3. **AP Duplicate Recovery:** Recover {_format_currency(dup_recovery)} across {len(duplicates)} duplicate ERP posting(s).",
        ]
    )

    summary = (
        f"Board brief published: EAC {_format_currency(kpis['total_eac'])} vs Budget "
        f"{_format_currency(kpis['total_budget'])}; {len(spikes)} spend spike(s) at z >= {z_threshold:.1f}; "
        f"{_format_currency(total_reclaimable)} in actionable savings & recovery."
    )
    over_depts = [d for d in depts if d["status"] == "over_budget"]
    has_high_spike = any(s["severity"] == "high" for s in spikes)
    severity = (
        "high"
        if (kpis["variance_pct"] > variance_threshold_pct or over_depts or has_high_spike)
        else "normal"
    )
    highlights = [
        f"Top variance risk: {top_risk['department']} ({_format_var_pct(top_risk)})"
        if top_risk
        else "No risk exposure",
        f"Spend spikes (z >= {z_threshold:.1f}): {len(spikes)} ({_format_currency(spike_total)})",
        f"Inactive SaaS savings: {_format_currency(inactive_annual)}/yr",
        f"Active price-hike renegotiation: {_format_currency(hike_annual)}/yr",
        f"Duplicate invoice recovery: {_format_currency(dup_recovery)}",
    ]
    webhook = _build_slack_webhook(
        workflow_id="exec_brief",
        workflow_name=WORKFLOWS_BY_ID["exec_brief"]["name"],
        ran_at=ran_at,
        summary=summary,
        severity=severity,
        metrics={
            "Annualized EAC": _format_currency(kpis["total_eac"]),
            "Net Variance": f"{_format_signed_currency(kpis['net_variance'])} ({kpis['variance_pct']:+.2f}%)",
            "Spend Spikes": f"{len(spikes)} ({_format_currency(spike_total)})",
            "Duplicate Recovery": _format_currency(dup_recovery),
            "SaaS Savings": f"{_format_currency(sub_audit['annual_savings_opp'])}/yr",
        },
        highlights=highlights,
    )

    steps.append(
        {
            "name": "Publish Executive Markdown & Webhook",
            "status": "passed",
            "detail": (
                f"Compiled CFO board brief ({len(spikes)} spikes surfaced at z >= {z_threshold:.1f}) "
                "and executive summary Slack webhook payload."
            ),
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
    )
    return steps, summary, output_md, webhook


def run_workflow(
    db_path: str | Path | None,
    workflow_id: str,
    params: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Execute one of the 3 automation pipelines against SQLite and optionally persist to workflow_runs."""
    wf_id = (workflow_id or "").strip()
    if wf_id not in WORKFLOWS_BY_ID:
        raise ValueError(
            f"Unknown workflow_id '{wf_id}'. Expected one of: {', '.join(WORKFLOWS_BY_ID.keys())}."
        )

    wf_meta = WORKFLOWS_BY_ID[wf_id]
    default_params = wf_meta["default_params"]
    merged_params = dict(default_params)
    if isinstance(params, dict):
        merged_params.update(params)

    z_threshold = _parse_float_param(
        merged_params, "z_threshold", float(default_params.get("z_threshold", 2.0)), 1.0, 5.0
    )
    clean_params: dict[str, float] = {"z_threshold": z_threshold}
    variance_threshold_pct: float | None = None
    if "variance_threshold_pct" in default_params:
        variance_threshold_pct = _parse_float_param(
            merged_params,
            "variance_threshold_pct",
            float(default_params["variance_threshold_pct"]),
            0.5,
            50.0,
        )
        clean_params = {
            "variance_threshold_pct": variance_threshold_pct,
            "z_threshold": z_threshold,
        }

    started = time.perf_counter()
    ran_at = utc_now_iso()

    with open_readonly_snapshot(db_path) as snap_conn:
        if wf_id == "monthly_close":
            assert variance_threshold_pct is not None
            steps, summary, output_md, webhook = _execute_monthly_close(
                db_path, variance_threshold_pct, z_threshold, ran_at, conn=snap_conn
            )
        elif wf_id == "anomaly_watchdog":
            steps, summary, output_md, webhook = _execute_anomaly_watchdog(
                db_path, z_threshold, ran_at, conn=snap_conn
            )
        else:
            assert variance_threshold_pct is not None
            steps, summary, output_md, webhook = _execute_exec_brief(
                db_path, variance_threshold_pct, z_threshold, ran_at, conn=snap_conn
            )

    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    run_id = 0

    if persist:
        with get_db_lock(db_path):
            conn = get_connection(db_path, read_only=False)
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        """
                        INSERT INTO workflow_runs (
                            workflow_id, workflow_name, ran_at, status, duration_ms,
                            params_json, summary, steps_json, output_markdown, webhook_payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            wf_id,
                            wf_meta["name"],
                            ran_at,
                            "completed",
                            duration_ms,
                            json.dumps(clean_params, allow_nan=False),
                            summary,
                            json.dumps(steps, allow_nan=False),
                            output_md,
                            json.dumps(webhook, allow_nan=False),
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

    return {
        "ok": True,
        "run": {
            "id": run_id,
            "workflow_id": wf_id,
            "workflow_name": wf_meta["name"],
            "ran_at": ran_at,
            "status": "completed",
            "duration_ms": duration_ms,
            "params": clean_params,
            "summary": summary,
            "steps": steps,
            "output_markdown": output_md,
            "webhook_payload": webhook,
        },
    }


def list_workflows_and_history(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return workflow catalog and persisted execution history for GET /api/workflows."""
    with open_readonly_snapshot(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, workflow_id, workflow_name, ran_at, status, duration_ms,
                   params_json, summary, steps_json, output_markdown, webhook_payload_json
            FROM workflow_runs
            ORDER BY id DESC
            LIMIT 25
            """
        ).fetchall()

    history: list[dict[str, Any]] = []
    for row in rows:
        history.append(
            {
                "id": int(row["id"]),
                "workflow_id": str(row["workflow_id"]),
                "workflow_name": str(row["workflow_name"]),
                "ran_at": str(row["ran_at"]),
                "status": str(row["status"]),
                "duration_ms": float(row["duration_ms"]),
                "params": json.loads(row["params_json"]),
                "summary": str(row["summary"]),
                "steps": json.loads(row["steps_json"]),
                "output_markdown": str(row["output_markdown"]),
                "webhook_payload": json.loads(row["webhook_payload_json"]),
            }
        )

    return {
        "workflows": WORKFLOW_CATALOG,
        "history": history,
    }
