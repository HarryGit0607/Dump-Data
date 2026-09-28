"""Insights for PGIPH_STG_TPA_UPLOAD, scoped to one office code.

Office code is the token before the first ``/`` in ``PSTU_POL_NO``
(5–6 digits). Example: ``771001/48/2013/371`` → office ``771001``.

The rest of the policy number is treated as ``dept / underwriting year / serial``.
Other breakdowns (amounts, status, hospital, member, claim dates) are used when
those columns exist on the table — Premia staging layouts vary by release.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence


DEFAULT_TABLE = "PGIPH_STG_TPA_UPLOAD"
DEFAULT_OFFICE = "771001"
IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

PREFERRED = {
    "policy": ("PSTU_POL_NO",),
    "claim": ("PSTU_CLM_NO", "PSTU_CLAIM_NO", "PSTU_TPA_CLM_NO", "PSTU_CLM_SYS_ID"),
    "amount": ("PSTU_CLM_AMT", "PSTU_CLAIM_AMT", "PSTU_EST_AMT", "PSTU_GROSS_AMT"),
    "approved": ("PSTU_APPR_AMT", "PSTU_APR_AMT", "PSTU_SANC_AMT"),
    "paid": ("PSTU_PAID_AMT", "PSTU_NET_AMT", "PSTU_SETTLE_AMT"),
    "status": ("PSTU_STATUS", "PSTU_CLM_STATUS", "PSTU_APPR_STATUS", "PSTU_REC_STATUS"),
    "claim_date": ("PSTU_CLM_DT", "PSTU_LOSS_DT", "PSTU_INTM_DT", "PSTU_HOSP_FM_DT", "PSTU_CR_DT"),
    "hospital": ("PSTU_HOSP_NAME", "PSTU_HOSP_CODE", "PSTU_PROV_NAME"),
    "member": ("PSTU_MEMB_CODE", "PSTU_MEMB_ID", "PSTU_CUST_CODE", "PSTU_ASSR_CODE"),
    "tpa": ("PSTU_TPA_CODE", "PSTU_TPA_ID"),
    "tpa_claim": ("PSTU_TPA_CLM_NO", "PSTU_EXT_CLM_NO"),
}


class TpaDashboardError(Exception):
    """The table cannot be read or has no policy-number column."""


def assert_ident(name: str) -> str:
    if not IDENT_RE.match(name or ""):
        raise TpaDashboardError(f"refusing to use {name!r} as a SQL identifier")
    return name


def office_from_pol_no(pol_no: Any) -> str:
    """First slash-separated token of the policy number (office code)."""
    text = "" if pol_no is None else str(pol_no).strip()
    if not text:
        return ""
    return text.split("/", 1)[0].strip()


def parse_pol_no(pol_no: Any) -> dict[str, str]:
    text = "" if pol_no is None else str(pol_no).strip()
    parts = [part.strip() for part in text.split("/") if part.strip() != ""]
    return {
        "raw": text,
        "office": parts[0] if parts else "",
        "dept": parts[1] if len(parts) > 1 else "",
        "year": parts[2] if len(parts) > 2 else "",
        "serial": parts[3] if len(parts) > 3 else "",
    }


def office_sql(pol_column: str) -> str:
    """SQL expression for the office token; works on Oracle and SQLite."""
    col = assert_ident(pol_column)
    return f"SUBSTR({col}, 1, INSTR({col} || '/', '/') - 1)"


def _upper_map(columns: Sequence[str]) -> dict[str, str]:
    return {name.upper(): name for name in columns}


def map_columns(columns: Sequence[str]) -> dict[str, str]:
    """Pick the best available column for each analytic role."""
    by_upper = _upper_map(columns)
    mapped: dict[str, str] = {}
    for role, candidates in PREFERRED.items():
        for candidate in candidates:
            if candidate in by_upper:
                mapped[role] = by_upper[candidate]
                break
        if role in mapped:
            continue
        needle = {
            "policy": "POL_NO",
            "claim": "CLM_NO",
            "amount": "_AMT",
            "approved": "APPR",
            "paid": "PAID",
            "status": "STATUS",
            "claim_date": "_DT",
            "hospital": "HOSP",
            "member": "MEMB",
            "tpa": "TPA_CODE",
            "tpa_claim": "TPA_CLM",
        }.get(role)
        if not needle:
            continue
        for upper, original in by_upper.items():
            if needle in upper and role not in mapped:
                mapped[role] = original
                break
    return mapped


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    for candidate in (text[:19], text[:11].strip(), text[:10]):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d-%b-%Y",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%Y%m%d",
        ):
            try:
                return dt.datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    try:
        return dt.datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def amount_bucket(amount: float) -> str:
    if amount < 0:
        return "negative"
    edges = (0, 1000, 5000, 10000, 25000, 50000, 100000)
    for index, edge in enumerate(edges):
        nxt = edges[index + 1] if index + 1 < len(edges) else None
        if nxt is None:
            return f"{edge:,}+"
        if edge <= amount < nxt:
            return f"{edge:,}–{nxt:,}"
    return "0–1,000"


BUCKET_ORDER = (
    "negative",
    "0–1,000",
    "1,000–5,000",
    "5,000–10,000",
    "10,000–25,000",
    "25,000–50,000",
    "50,000–100,000",
    "100,000+",
)


def _get(row: Mapping[str, Any], mapped: Mapping[str, str], role: str) -> Any:
    column = mapped.get(role)
    if not column:
        return None
    if column in row:
        return row[column]
    # Oracle may return uppercase keys; sqlite keeps created case.
    upper = {str(key).upper(): key for key in row}
    real = upper.get(column.upper())
    return row.get(real) if real is not None else None


def _round_money(value: float) -> float:
    return round(value, 2)


def _sorted_counter(counter: Mapping[str, int], *, limit: Optional[int] = None) -> list[dict[str, Any]]:
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None:
        items = items[:limit]
    return [{"name": name, "count": count} for name, count in items]


def compute_insights(
    rows: Iterable[Mapping[str, Any]],
    *,
    office: str = DEFAULT_OFFICE,
    table: str = DEFAULT_TABLE,
    columns: Sequence[str] = (),
    mapped: Optional[Mapping[str, str]] = None,
    source: str = "oracle",
    table_rows: Optional[int] = None,
    generated_at: Optional[str] = None,
    error: Optional[str] = None,
    reachable: bool = True,
) -> dict[str, Any]:
    """Aggregate office-scoped rows into the dashboard payload."""
    mapped = dict(mapped or (map_columns(columns) if columns else {}))
    office = str(office)
    claims = 0
    policies: Counter[str] = Counter()
    claim_ids: set[str] = set()
    members: Counter[str] = Counter()
    hospitals: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    depts: Counter[str] = Counter()
    years: Counter[str] = Counter()
    months: Counter[str] = Counter()
    claim_years: Counter[str] = Counter()
    tpas: Counter[str] = Counter()
    policy_amounts: dict[str, float] = defaultdict(float)
    policy_claim_ids: dict[str, set[str]] = defaultdict(set)
    member_claims: dict[str, int] = defaultdict(int)
    year_amount: dict[str, float] = defaultdict(float)
    year_policies: dict[str, set[str]] = defaultdict(set)
    month_amount: dict[str, float] = defaultdict(float)
    total_amount = 0.0
    approved_amount = 0.0
    paid_amount = 0.0
    amount_count = 0
    dates: list[dt.date] = []
    malformed = 0
    other_office = 0
    buckets: Counter[str] = Counter()

    scoped: list[Mapping[str, Any]] = []
    for row in rows:
        parsed = parse_pol_no(_get(row, mapped, "policy") if mapped else row.get("PSTU_POL_NO"))
        if parsed["office"] != office:
            if parsed["office"]:
                other_office += 1
            else:
                malformed += 1
            continue
        if not parsed["year"] and parsed["raw"].count("/") < 2:
            malformed += 1
        scoped.append(row)
        claims += 1
        pol = parsed["raw"]
        policies[pol] += 1
        if parsed["dept"]:
            depts[parsed["dept"]] += 1
        if parsed["year"]:
            years[parsed["year"]] += 1
            year_policies[parsed["year"]].add(pol)

        claim_no = _get(row, mapped, "claim")
        if claim_no not in (None, ""):
            claim_ids.add(str(claim_no))
            policy_claim_ids[pol].add(str(claim_no))

        member = _get(row, mapped, "member")
        if member not in (None, ""):
            members[str(member)] += 1
            member_claims[str(member)] += 1

        hospital = _get(row, mapped, "hospital")
        if hospital not in (None, ""):
            hospitals[str(hospital)] += 1

        status = _get(row, mapped, "status")
        if status not in (None, ""):
            statuses[str(status)] += 1

        tpa = _get(row, mapped, "tpa")
        if tpa not in (None, ""):
            tpas[str(tpa)] += 1

        amount = _number(_get(row, mapped, "amount"))
        if amount is not None:
            total_amount += amount
            amount_count += 1
            policy_amounts[pol] += amount
            if parsed["year"]:
                year_amount[parsed["year"]] += amount
            buckets[amount_bucket(amount)] += 1

        approved = _number(_get(row, mapped, "approved"))
        if approved is not None:
            approved_amount += approved
        paid = _number(_get(row, mapped, "paid"))
        if paid is not None:
            paid_amount += paid

        when = _date(_get(row, mapped, "claim_date"))
        if when is not None:
            dates.append(when)
            month_key = when.strftime("%Y-%m")
            months[month_key] += 1
            if amount is not None:
                month_amount[month_key] += amount
            claim_years[str(when.year)] += 1

    repeats = []
    for pol, count in policies.most_common():
        if count < 2:
            continue
        repeats.append(
            {
                "policy": pol,
                "claims": count,
                "distinct_claim_ids": len(policy_claim_ids.get(pol, ())),
                "amount": _round_money(policy_amounts.get(pol, 0.0)),
                "dept": parse_pol_no(pol)["dept"],
                "year": parse_pol_no(pol)["year"],
            }
        )

    member_repeats = [
        {"member": member, "claims": count}
        for member, count in members.most_common()
        if count >= 2
    ]

    by_year = []
    for year in sorted(set(years) | set(claim_years)):
        by_year.append(
            {
                "year": year,
                "claims": years.get(year, 0),
                "policies": len(year_policies.get(year, ())),
                "amount": _round_money(year_amount.get(year, 0.0)),
                "claim_date_rows": claim_years.get(year, 0),
            }
        )

    by_month = [
        {
            "month": month,
            "claims": months[month],
            "amount": _round_money(month_amount.get(month, 0.0)),
        }
        for month in sorted(months)
    ]

    avg_amount = _round_money(total_amount / amount_count) if amount_count else 0.0
    distinct_policies = len(policies)
    repeat_policies = sum(1 for count in policies.values() if count >= 2)
    repeat_share = round(100.0 * repeat_policies / distinct_policies, 1) if distinct_policies else 0.0

    yoy = []
    for index in range(1, len(by_year)):
        prev = by_year[index - 1]["claims"]
        curr = by_year[index]["claims"]
        change = None if not prev else round(100.0 * (curr - prev) / prev, 1)
        yoy.append({"from": by_year[index - 1]["year"], "to": by_year[index]["year"], "pct": change})

    payload = {
        "meta": {
            "office": office,
            "table": table,
            "source": source,
            "reachable": reachable,
            "error": error,
            "generated_at": generated_at
            or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "filter": (
                f"SUBSTR(PSTU_POL_NO, 1, INSTR(PSTU_POL_NO || '/', '/') - 1) = '{office}'"
            ),
            "example": "771001/48/2013/371 → office 771001, dept 48, year 2013, serial 371",
            "columns": list(columns),
            "mapped": dict(mapped),
            "table_rows": table_rows,
            "scoped_rows": claims,
        },
        "kpis": {
            "claims": claims,
            "distinct_policies": distinct_policies,
            "distinct_claim_ids": len(claim_ids),
            "repeat_policies": repeat_policies,
            "repeat_share_pct": repeat_share,
            "claims_per_policy": round(claims / distinct_policies, 2) if distinct_policies else 0.0,
            "total_amount": _round_money(total_amount),
            "approved_amount": _round_money(approved_amount),
            "paid_amount": _round_money(paid_amount),
            "avg_amount": avg_amount,
            "amount_rows": amount_count,
            "date_from": min(dates).isoformat() if dates else None,
            "date_to": max(dates).isoformat() if dates else None,
        },
        "by_year": by_year,
        "year_over_year": yoy,
        "by_month": by_month,
        "by_dept": _sorted_counter(depts),
        "by_status": _sorted_counter(statuses),
        "by_tpa": _sorted_counter(tpas),
        "repeats": repeats[:100],
        "repeat_count": len(repeats),
        "member_repeats": member_repeats[:50],
        "top_hospitals": _sorted_counter(hospitals, limit=15),
        "top_members": _sorted_counter(members, limit=15),
        "amount_buckets": [
            {"name": name, "count": buckets[name]}
            for name in BUCKET_ORDER
            if buckets.get(name)
        ],
        "quality": {
            "malformed_policy_numbers": malformed,
            "rows_other_offices_seen": other_office,
            "policies_single_claim": sum(1 for count in policies.values() if count == 1),
            "max_claims_on_one_policy": max(policies.values()) if policies else 0,
        },
    }
    return payload


def insights_from_csv(
    path: str,
    *,
    office: str = DEFAULT_OFFICE,
    table: str = DEFAULT_TABLE,
) -> dict[str, Any]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        rows = list(reader)
    return compute_insights(
        rows,
        office=office,
        table=table,
        columns=columns,
        mapped=map_columns(columns),
        source=f"csv:{path}",
        table_rows=len(rows),
    )


def dump_insights(payload: Mapping[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")
