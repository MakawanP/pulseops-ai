"""Deterministic tool-calling AI Copilot with authentic SQL traces and optional live LLM synthesis."""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from backend.analytics import (
    check_subscriptions,
    compute_department_variance,
    detect_all_anomalies,
    get_overview,
)
from backend.database import execute_readonly_sql, open_readonly_snapshot

VALID_PROVIDERS = ("local", "gemini", "openai", "anthropic")

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"

MAX_LLM_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB safety cap on external LLM payloads
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_DEPT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:engineering|eng)\b", re.IGNORECASE), "Engineering"),
    (re.compile(r"\b(?:marketing|mktg|mkt)\b", re.IGNORECASE), "Marketing"),
    (re.compile(r"\bsales\b", re.IGNORECASE), "Sales"),
    (re.compile(r"\b(?:operations|ops)\b", re.IGNORECASE), "Operations"),
    (
        re.compile(
            r"(?<!\w)g&a(?!\w)|\bgeneral\s+(?:&|and)\s+admin(?:istration)?\b",
            re.IGNORECASE,
        ),
        "G&A",
    ),
)

_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "brief": re.compile(r"\b(?:brief|executive|board|close|q4|overview|cfo)\b", re.IGNORECASE),
    "variance": re.compile(
        r"\b(?:variance|budget|over[- ]budget|overspend|eac|forecast|departments?|spend|spending|total)\b",
        re.IGNORECASE,
    ),
    "anomalies": re.compile(
        r"\b(?:anomal\w*|spikes?|outliers?|duplicates?|fraud|audit\w*|invoices?|why)\b",
        re.IGNORECASE,
    ),
    "subscriptions": re.compile(
        r"\b(?:saas|subscriptions?|renewals?|unused|inactive|licen[cs]es?|price\s+hikes?|savings?|watchdog)\b",
        re.IGNORECASE,
    ),
    "vendors": re.compile(r"\b(?:vendors?|top|largest|biggest)\b", re.IGNORECASE),
    "summary": re.compile(r"\b(?:summary|summarize)\b", re.IGNORECASE),
}

_WHY_RE = re.compile(r"\bwhy\b", re.IGNORECASE)


def get_llm_provider_status() -> dict[str, bool]:
    """Check whether optional external LLM API keys are configured in environment variables."""
    return {
        "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
        "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    }


def _fmt_money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _fmt_signed_money(amount: float) -> str:
    sign = "+" if amount > 0 else ("-" if amount < 0 else "")
    return f"{sign}${abs(amount):,.2f}"


def _fmt_dept_var_pct(dept: dict[str, Any]) -> str:
    if dept.get("unbudgeted"):
        return "Unbudgeted"
    return f"{dept['variance_pct']:+.2f}%"


def _resolve_model(env_var: str, default: str) -> str:
    model = os.environ.get(env_var, "").strip() or default
    if not _MODEL_NAME_RE.match(model):
        raise ValueError(f"Invalid model identifier configured in {env_var}.")
    return model


def _validate_api_key(api_key: str) -> str:
    clean = (api_key or "").strip()
    if not clean or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in clean):
        raise ValueError("Invalid API key format.")
    return clean


class _CopilotToolbox:
    """Explicit tool registry recording every tool call, authentic executed SQL, and latency."""

    def __init__(self, db_path: str | Path | None, conn: sqlite3.Connection) -> None:
        self.db_path = db_path
        self.conn = conn
        self.trace: list[dict[str, Any]] = []

    def _record(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        output_summary: str,
        sql: str | None,
        method: str,
        started: float,
    ) -> None:
        self.trace.append(
            {
                "step": len(self.trace) + 1,
                "tool": tool_name,
                "method": method,
                "input": tool_input,
                "output_summary": output_summary,
                "sql": sql,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }
        )

    def query_sql_readonly(self, sql: str) -> dict[str, Any]:
        started = time.perf_counter()
        result = execute_readonly_sql(self.db_path, sql, max_rows=50)
        summary = (
            f"Returned {result['row_count']} row(s) across {len(result['columns'])} column(s) "
            f"[{', '.join(result['columns'])}]."
        )
        self._record(
            "query_sql_readonly",
            {"sql": sql},
            summary,
            sql,
            "sqlite_readonly",
            started,
        )
        return result

    def compute_department_variance(
        self,
        departments: list[str] | None = None,
        variance_threshold_pct: float = 5.0,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Compute variance/EAC for all departments and return (scoped_rows, all_rows)."""
        started = time.perf_counter()
        all_rows = compute_department_variance(
            self.db_path,
            department=None,
            variance_threshold_pct=variance_threshold_pct,
            conn=self.conn,
        )
        if departments:
            wanted = {d.lower() for d in departments}
            rows = [r for r in all_rows if r["department"].lower() in wanted]
        else:
            rows = all_rows

        over = [r["department"] for r in rows if r["status"] == "over_budget"]
        watch = [r["department"] for r in rows if r["status"] == "watch"]
        scope_input: str | list[str] | None = (
            None if not departments else (departments[0] if len(departments) == 1 else list(departments))
        )
        summary = (
            f"Computed variance & EAC for {len(rows)} department(s): "
            f"{len(over)} over_budget ({', '.join(over) or 'none'}), "
            f"{len(watch)} watch ({', '.join(watch) or 'none'})."
        )
        self._record(
            "compute_department_variance",
            {"department": scope_input, "variance_threshold_pct": variance_threshold_pct},
            summary,
            None,
            "python_analytics",
            started,
        )
        return rows, all_rows

    def detect_anomalies(
        self,
        z_threshold: float = 2.0,
        departments: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        if departments and len(departments) == 1:
            anomalies = detect_all_anomalies(
                self.db_path,
                z_threshold=z_threshold,
                department=departments[0],
                conn=self.conn,
            )
            scope_input: str | list[str] | None = departments[0]
        else:
            all_anomalies = detect_all_anomalies(
                self.db_path,
                z_threshold=z_threshold,
                department=None,
                conn=self.conn,
            )
            if departments:
                wanted = {d.lower() for d in departments}
                anomalies = [a for a in all_anomalies if a["department"].lower() in wanted]
                scope_input = list(departments)
            else:
                anomalies = all_anomalies
                scope_input = None

        dups = [a for a in anomalies if a["type"] == "duplicate_invoice"]
        spikes = [a for a in anomalies if a["type"] == "spend_spike"]
        summary = (
            f"Found {len(anomalies)} total anomaly(ies): {len(dups)} duplicate invoice(s) "
            f"and {len(spikes)} statistical spend spike(s) (z >= {z_threshold})."
        )
        self._record(
            "detect_anomalies",
            {"z_threshold": z_threshold, "department": scope_input},
            summary,
            None,
            "python_analytics",
            started,
        )
        return anomalies

    def check_subscriptions(self) -> dict[str, Any]:
        started = time.perf_counter()
        result = check_subscriptions(self.db_path, conn=self.conn)
        summary = (
            f"Audited {len(result['subscriptions'])} SaaS contracts: "
            f"{len(result['price_hikes'])} price hike(s) >=15%, "
            f"{len(result['inactive'])} inactive >90d; "
            f"annualized savings opportunity {_fmt_money(result['annual_savings_opp'])}."
        )
        self._record(
            "check_subscriptions",
            {"hike_threshold_pct": 15.0, "inactive_days_threshold": 90},
            summary,
            None,
            "python_analytics",
            started,
        )
        return result

    def draft_executive_brief(self, focus: str = "full") -> dict[str, Any]:
        started = time.perf_counter()
        overview = get_overview(self.db_path, conn=self.conn)
        kpis = overview["kpis"]
        summary = (
            f"Compiled executive KPI snapshot: Actual {_fmt_money(kpis['total_actual'])} vs "
            f"Budget {_fmt_money(kpis['total_budget'])} ({kpis['variance_pct']:+.2f}%), "
            f"EAC {_fmt_money(kpis['total_eac'])}, {kpis['anomaly_count']} anomalies."
        )
        self._record(
            "draft_executive_brief",
            {"focus": focus},
            summary,
            None,
            "python_analytics",
            started,
        )
        return overview


def _detect_mentioned_departments(question: str) -> list[str]:
    """Extract canonical department names mentioned in the question using strict word boundaries."""
    matches: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pattern, canonical in _DEPT_PATTERNS:
        m = pattern.search(question)
        if m and canonical not in seen:
            seen.add(canonical)
            matches.append((m.start(), canonical))
    matches.sort(key=lambda item: item[0])
    return [canonical for _, canonical in matches]


def _classify_intents(question: str) -> set[str]:
    intents: set[str] = set()
    for tag in ("brief", "variance", "anomalies", "subscriptions", "vendors"):
        if _INTENT_PATTERNS[tag].search(question):
            intents.add(tag)
    if not intents and _INTENT_PATTERNS["summary"].search(question):
        intents.add("brief")
    if not intents:
        intents = {"variance", "anomalies", "subscriptions"}
    return intents


def _build_kpis_summary(
    all_dept_rows: list[dict[str, Any]],
    sub_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute portfolio KPIs directly from already-loaded analytics results without extra DB queries."""
    total_budget = round(sum(d["budget"] for d in all_dept_rows), 2)
    total_actual = round(sum(d["actual"] for d in all_dept_rows), 2)
    net_variance = round(total_actual - total_budget, 2)
    variance_pct = round((net_variance / total_budget) * 100.0, 2) if total_budget > 0 else 0.0
    total_eac = round(sum(d["eac"] for d in all_dept_rows), 2)
    return {
        "total_budget": total_budget,
        "total_actual": total_actual,
        "net_variance": net_variance,
        "variance_pct": variance_pct,
        "total_eac": total_eac,
        "subscription_savings_opp": sub_audit["annual_savings_opp"] if sub_audit else 0.0,
    }


def _build_local_answer(
    question: str,
    intents: set[str],
    target_depts: list[str],
    kpis: dict[str, Any],
    dept_rows: list[dict[str, Any]] | None,
    anomalies: list[dict[str, Any]] | None,
    sub_audit: dict[str, Any] | None,
    vendor_sql_res: dict[str, Any] | None,
) -> tuple[str, list[dict[str, str]], list[str]]:
    """Build a strictly grounded Markdown response, verified citations, and contextual follow-ups."""
    sections: list[str] = []
    citations: list[dict[str, str]] = []

    if "brief" in intents or "variance" in intents:
        citations.extend(
            [
                {
                    "metric": "Total FY Budget",
                    "value": _fmt_money(kpis["total_budget"]),
                    "source": "budgets (SQL SUM)",
                },
                {
                    "metric": "Total Actual Spend",
                    "value": _fmt_money(kpis["total_actual"]),
                    "source": "transactions (SQL SUM)",
                },
                {
                    "metric": "Net Variance",
                    "value": f"{_fmt_signed_money(kpis['net_variance'])} ({kpis['variance_pct']:+.2f}%)",
                    "source": "compute_department_variance",
                },
                {
                    "metric": "Annualized EAC",
                    "value": _fmt_money(kpis["total_eac"]),
                    "source": "compute_department_variance",
                },
            ]
        )

    if "brief" in intents:
        over = [d for d in (dept_rows or []) if d["status"] == "over_budget"]
        dups = [a for a in (anomalies or []) if a["type"] == "duplicate_invoice"]
        spikes = [a for a in (anomalies or []) if a["type"] == "spend_spike"]
        dup_exp = round(sum(d["amount"] for d in dups), 2)
        annual_saas = sub_audit["annual_savings_opp"] if sub_audit else kpis["subscription_savings_opp"]

        citations.append(
            {
                "metric": "SaaS Savings Opportunity",
                "value": f"{_fmt_money(annual_saas)}/yr",
                "source": "check_subscriptions",
            }
        )
        sections.append(
            "### Executive Financial & Operational Brief\n"
            f"- **Consolidated Spend:** {_fmt_money(kpis['total_actual'])} actual vs. "
            f"{_fmt_money(kpis['total_budget'])} budget (**{_fmt_signed_money(kpis['net_variance'])}**, "
            f"**{kpis['variance_pct']:+.2f}%**). Q4-weighted annualized EAC stands at **{_fmt_money(kpis['total_eac'])}**.\n"
            f"- **Over-Budget Exposure:** {len(over)} department(s) exceed the 5% threshold — "
            + (
                ", ".join(
                    f"**{d['department']}** ({_fmt_dept_var_pct(d)}, {_fmt_signed_money(d['variance'])})"
                    for d in over
                )
                if over
                else "none."
            )
            + "\n"
            f"- **Audit & Risk Findings:** **{len(dups)} duplicate invoice(s)** totaling **{_fmt_money(dup_exp)}** "
            f"and **{len(spikes)} statistical spend spike(s)**.\n"
            f"- **Actionable Cost Recovery:** **{_fmt_money(dup_exp + annual_saas)}** combined "
            f"({_fmt_money(dup_exp)} AP duplicate recovery + {_fmt_money(annual_saas)}/yr SaaS optimization)."
        )

    if dept_rows is not None and ("variance" in intents or "brief" in intents):
        if len(target_depts) == 1:
            heading = f"### {target_depts[0]} Budget vs. Actuals"
        elif len(target_depts) >= 2:
            heading = f"### {' vs. '.join(target_depts)} Budget vs. Actuals"
        else:
            heading = "### Department Variance & EAC Breakdown"
        lines = [heading]
        if not target_depts and "brief" not in intents:
            lines.append(
                f"- **Consolidated Portfolio:** **{_fmt_money(kpis['total_actual'])}** actual vs. "
                f"**{_fmt_money(kpis['total_budget'])}** budget (**{_fmt_signed_money(kpis['net_variance'])}**, "
                f"**{kpis['variance_pct']:+.2f}%**) · Total Annualized EAC **{_fmt_money(kpis['total_eac'])}**"
            )
        for d in dept_rows:
            lines.append(
                f"- **{d['department']} (`{d['status']}`):** Actual **{_fmt_money(d['actual'])}** vs. "
                f"Budget {_fmt_money(d['budget'])} (**{_fmt_signed_money(d['variance'])}**, "
                f"**{_fmt_dept_var_pct(d)}**) · EAC {_fmt_money(d['eac'])}"
            )
            if d["status"] in ("over_budget", "watch") or target_depts:
                citations.append(
                    {
                        "metric": f"{d['department']} Variance",
                        "value": f"{_fmt_signed_money(d['variance'])} ({_fmt_dept_var_pct(d)})",
                        "source": "compute_department_variance",
                    }
                )
        sections.append("\n".join(lines))

    if vendor_sql_res is not None and vendor_sql_res.get("rows"):
        v_lines = ["### Top Spend Drivers (Verified SQL)"]
        for row in vendor_sql_res["rows"][:5]:
            vendor_name, dept_name, total_spend, inv_cnt = row[0], row[1], float(row[2]), int(row[3])
            v_lines.append(
                f"- **{vendor_name}** ({dept_name}): **{_fmt_money(total_spend)}** across {inv_cnt} invoice(s)"
            )
        top_row = vendor_sql_res["rows"][0]
        citations.append(
            {
                "metric": f"Top Vendor ({top_row[0]})",
                "value": _fmt_money(float(top_row[2])),
                "source": "query_sql_readonly",
            }
        )
        sections.append("\n".join(v_lines))

    if anomalies is not None and (
        "anomalies" in intents or ("variance" in intents and _WHY_RE.search(question))
    ):
        dups = [a for a in anomalies if a["type"] == "duplicate_invoice"]
        spikes = [a for a in anomalies if a["type"] == "spend_spike"]
        dup_exp = round(sum(a["amount"] for a in dups), 2)

        citations.append(
            {
                "metric": "Duplicate Invoice Exposure",
                "value": f"{_fmt_money(dup_exp)} ({len(dups)} invoice(s))",
                "source": "detect_anomalies",
            }
        )
        citations.append(
            {
                "metric": "Statistical Spend Spikes",
                "value": f"{len(spikes)} outlier(s)",
                "source": "detect_anomalies",
            }
        )

        a_lines = ["### Duplicate Invoices & Spend Spike Audit"]
        if dups:
            a_lines.append("**Duplicate ERP Invoices (Recoverable Exposure):**")
            for d in dups:
                a_lines.append(
                    f"- `{d['invoice_id']}` — **{d['vendor']}** ({d['department']}): "
                    f"**{_fmt_money(d['amount'])}** ({d['detail']})"
                )
        if spikes:
            a_lines.append("**Statistical Spend Outliers (Z-Score + IQR):**")
            for s in spikes[:6]:
                a_lines.append(
                    f"- `{s['invoice_id']}` — **{s['vendor']}** ({s['department']} · {s['tx_date']}): "
                    f"**{_fmt_money(s['amount'])}** [{s['severity']}] — {s['detail']}"
                )
        if not dups and not spikes:
            a_lines.append("- No duplicate invoices or statistical spend spikes detected in scope.")
        sections.append("\n".join(a_lines))

    if sub_audit is not None and ("subscriptions" in intents or "brief" in intents):
        hikes = sub_audit["price_hikes"]
        inactive = sub_audit["inactive"]
        annual_opp = sub_audit["annual_savings_opp"]
        monthly_opp = sub_audit["monthly_savings_opp"]

        citations.append(
            {
                "metric": "SaaS Annual Savings Opp",
                "value": _fmt_money(annual_opp),
                "source": "check_subscriptions",
            }
        )
        citations.append(
            {
                "metric": "Flagged SaaS Contracts",
                "value": f"{sub_audit['flagged_count']} ({len(hikes)} hikes, {len(inactive)} inactive >90d)",
                "source": "check_subscriptions",
            }
        )

        s_lines = [
            "### SaaS Subscription Watchdog & Savings Opportunity",
            f"- **Total Reclaimable Opportunity:** **{_fmt_money(annual_opp)}/yr** ({_fmt_money(monthly_opp)}/mo)",
        ]
        if inactive:
            s_lines.append("**Inactive Licenses (>90 Days Unused — Immediate Cancellation Candidates):**")
            for sub in inactive:
                s_lines.append(
                    f"- **{sub['vendor']}** ({sub['department']}): **{_fmt_money(sub['monthly_cost'])}/mo** "
                    f"({_fmt_money(sub['monthly_cost'] * 12)}/yr) — unused for **{sub['last_used_days']} days**"
                )
        if hikes:
            s_lines.append("**Renewal Price Hikes (>=15% Increase — Renegotiation Targets):**")
            for sub in hikes:
                delta = sub["monthly_cost"] - sub["prev_monthly_cost"]
                s_lines.append(
                    f"- **{sub['vendor']}** ({sub['department']}): {_fmt_money(sub['prev_monthly_cost'])} → "
                    f"**{_fmt_money(sub['monthly_cost'])}/mo** (**+{sub['price_change_pct']:.1f}%**, "
                    f"+{_fmt_money(delta * 12)}/yr)"
                )
        sections.append("\n".join(s_lines))

    dedup_citations: list[dict[str, str]] = []
    seen_metrics: set[str] = set()
    for c in citations:
        if c["metric"] not in seen_metrics:
            seen_metrics.add(c["metric"])
            dedup_citations.append(c)

    followups = _suggest_followups(intents)
    return "\n\n".join(sections), dedup_citations[:12], followups


def _suggest_followups(intents: set[str]) -> list[str]:
    pool = [
        ("variance", "Which departments are over budget and why?"),
        ("anomalies", "Audit duplicate invoices and spend spikes"),
        ("subscriptions", "Summarize SaaS subscription savings and price hikes"),
        ("brief", "Draft a Q4 executive variance brief for the CFO"),
        ("vendors", "Who are our top 5 vendors by total FY spend?"),
    ]
    suggestions = [prompt for tag, prompt in pool if tag not in intents]
    if len(suggestions) < 3:
        for _, prompt in pool:
            if prompt not in suggestions:
                suggestions.append(prompt)
    return suggestions[:3]


def _read_bounded_json(resp: Any) -> dict[str, Any]:
    raw = resp.read(MAX_LLM_RESPONSE_BYTES + 1)
    if len(raw) > MAX_LLM_RESPONSE_BYTES:
        raise RuntimeError("External LLM response exceeded size limit.")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Malformed external LLM JSON payload.")
    return data


def _synthesize_with_external_llm(
    provider: str,
    api_key: str,
    question: str,
    grounded_context: str,
) -> str:
    """Optionally polish/synthesize grounded tool outputs via Gemini, OpenAI, or Anthropic REST API."""
    clean_key = _validate_api_key(api_key)
    system_prompt = (
        "You are PulseOps AI, a senior FinOps & FP&A copilot. "
        "Synthesize a crisp, executive Markdown answer to the user's question using ONLY the verified "
        "SQL/Python tool outputs below. NEVER invent, alter, or round away any dollar figure, invoice ID, "
        "percentage, or vendor name."
    )
    user_prompt = f"User Question: {question}\n\nVerified Deterministic Tool Outputs:\n{grounded_context}"

    if provider == "gemini":
        model = _resolve_model("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        payload = {
            "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
            "generationConfig": {"temperature": 0.15, "maxOutputTokens": 900},
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": clean_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _read_bounded_json(resp)
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Empty Gemini candidates.")
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        content = first.get("content") if isinstance(first.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        text = "".join(
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ).strip()
        if not text:
            raise RuntimeError("Empty Gemini response.")
        return text

    if provider == "openai":
        model = _resolve_model("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": model,
            "temperature": 0.15,
            "max_tokens": 900,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {clean_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _read_bounded_json(resp)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Empty OpenAI choices.")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        raw_content = message.get("content")
        text = raw_content.strip() if isinstance(raw_content, str) else ""
        if not text:
            raise RuntimeError("Empty OpenAI response.")
        return text

    if provider == "anthropic":
        model = _resolve_model("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 900,
            "temperature": 0.15,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": clean_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _read_bounded_json(resp)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RuntimeError("Malformed Anthropic content.")
        text = "".join(
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ).strip()
        if not text:
            raise RuntimeError("Empty Anthropic response.")
        return text

    raise ValueError(f"Unsupported provider: {provider}")


def ask_copilot(
    db_path: str | Path | None,
    question: str,
    provider: str = "local",
    api_key: str = "",
) -> dict[str, Any]:
    """Run deterministic tool-calling planner + optional live LLM synthesis for POST /api/copilot/ask."""
    q = (question or "").strip()
    if not q:
        raise ValueError("Question cannot be empty.")

    prov = (provider or "local").strip().lower()
    if prov not in VALID_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{prov}'. Choose one of: {', '.join(VALID_PROVIDERS)}."
        )

    intents = _classify_intents(q)
    target_depts = _detect_mentioned_departments(q)

    dept_rows: list[dict[str, Any]] | None = None
    all_dept_rows: list[dict[str, Any]] | None = None
    anomalies: list[dict[str, Any]] | None = None
    sub_audit: dict[str, Any] | None = None
    vendor_sql_res: dict[str, Any] | None = None
    kpis: dict[str, Any] = {
        "total_budget": 0.0,
        "total_actual": 0.0,
        "net_variance": 0.0,
        "variance_pct": 0.0,
        "total_eac": 0.0,
        "subscription_savings_opp": 0.0,
    }

    with open_readonly_snapshot(db_path) as snap_conn:
        toolbox = _CopilotToolbox(db_path, snap_conn)

        if "variance" in intents or "brief" in intents:
            dept_rows, all_dept_rows = toolbox.compute_department_variance(
                departments=target_depts or None
            )

        if (
            "anomalies" in intents
            or ("variance" in intents and _WHY_RE.search(q))
            or "brief" in intents
        ):
            anomalies = toolbox.detect_anomalies(
                z_threshold=2.0, departments=target_depts or None
            )

        if "subscriptions" in intents or "brief" in intents:
            sub_audit = toolbox.check_subscriptions()

        if "vendors" in intents or "variance" in intents or "anomalies" in intents:
            if len(target_depts) == 1:
                safe_dept = target_depts[0].replace("'", "''")
                vendor_sql = (
                    "SELECT vendor, department, ROUND(SUM(amount), 2) AS total_spend, COUNT(*) AS invoice_count "
                    f"FROM transactions WHERE department = '{safe_dept}' "
                    "GROUP BY vendor, department ORDER BY total_spend DESC LIMIT 5"
                )
            elif len(target_depts) >= 2:
                dept_list = ", ".join("'" + d.replace("'", "''") + "'" for d in target_depts)
                vendor_sql = (
                    "SELECT vendor, department, ROUND(SUM(amount), 2) AS total_spend, COUNT(*) AS invoice_count "
                    f"FROM transactions WHERE department IN ({dept_list}) "
                    "GROUP BY vendor, department ORDER BY total_spend DESC LIMIT 5"
                )
            else:
                vendor_sql = (
                    "SELECT vendor, department, ROUND(SUM(amount), 2) AS total_spend, COUNT(*) AS invoice_count "
                    "FROM transactions GROUP BY vendor, department ORDER BY total_spend DESC LIMIT 5"
                )
            vendor_sql_res = toolbox.query_sql_readonly(vendor_sql)

        if "brief" in intents:
            focus_label = ", ".join(target_depts) if target_depts else "full"
            overview = toolbox.draft_executive_brief(focus=focus_label)
            kpis = overview["kpis"]
            if dept_rows is None:
                dept_rows = overview["department_variance"]
            if anomalies is None:
                anomalies = overview["anomalies"]
        elif all_dept_rows is not None:
            kpis = _build_kpis_summary(all_dept_rows, sub_audit=sub_audit)

        # Guarantee at least one authentic executed SQL verification call in tool_trace
        if not any(step["tool"] == "query_sql_readonly" for step in toolbox.trace):
            if "subscriptions" in intents:
                toolbox.query_sql_readonly(
                    "SELECT department, COUNT(*) AS contracts, ROUND(SUM(monthly_cost), 2) AS monthly_spend "
                    "FROM subscriptions GROUP BY department ORDER BY monthly_spend DESC"
                )
            else:
                toolbox.query_sql_readonly(
                    "SELECT department, ROUND(SUM(amount), 2) AS total_actual, COUNT(*) AS tx_count "
                    "FROM transactions GROUP BY department ORDER BY total_actual DESC"
                )

    local_md, citations, followups = _build_local_answer(
        question=q,
        intents=intents,
        target_depts=target_depts,
        kpis=kpis,
        dept_rows=dept_rows,
        anomalies=anomalies,
        sub_audit=sub_audit,
        vendor_sql_res=vendor_sql_res,
    )

    env_key_map = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    resolved_key = (api_key or "").strip() or os.environ.get(env_key_map.get(prov, ""), "").strip()

    answer_md = local_md
    mode = "local_grounded"
    mode_note = (
        "Answered via Local Grounded Tool Engine (100% deterministic SQL/Python math; zero external API keys required)."
    )

    if prov != "local":
        if not resolved_key:
            mode = "local_fallback"
            mode_note = (
                f"No API key provided for {prov.capitalize()}; seamlessly answered using Local Grounded Tool Engine."
            )
        else:
            try:
                answer_md = _synthesize_with_external_llm(prov, resolved_key, q, local_md)
                mode = f"{prov}_synthesized"
                mode_note = (
                    f"Synthesized via live {prov.capitalize()} API strictly grounded on deterministic SQL/Python tool outputs."
                )
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                RuntimeError,
                ValueError,
                OSError,
                json.JSONDecodeError,
                KeyError,
                IndexError,
                TypeError,
                AttributeError,
            ) as exc:
                mode = "local_fallback"
                mode_note = (
                    f"{prov.capitalize()} API unavailable ({exc.__class__.__name__}); "
                    "fell back cleanly to Local Grounded Tool Engine with verified tool citations."
                )

    return {
        "ok": True,
        "mode": mode,
        "mode_note": mode_note,
        "answer_markdown": answer_md,
        "citations": citations,
        "tool_trace": toolbox.trace,
        "suggested_followups": followups,
    }
