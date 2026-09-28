const money = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const whole = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

function $(id) {
  return document.getElementById(id);
}

function hideIfEmpty(el, items) {
  const card = el && el.closest(".card");
  if (card) card.style.display = items && items.length ? "" : "none";
}

function fmtMoney(value) {
  return money.format(value || 0);
}

function setBanner(payload) {
  const banner = $("banner");
  if (payload.meta.error) {
    banner.className = "banner";
    banner.textContent = payload.meta.error;
    return;
  }
  if (payload.meta.source && String(payload.meta.source).startsWith("csv")) {
    banner.className = "banner";
    banner.textContent = "Loaded from a file, not live Qc. Numbers are only as trustworthy as that extract.";
    return;
  }
  if (payload.meta.source && payload.meta.source.startsWith("sqlite")) {
    banner.className = "banner";
    banner.textContent = "This extract is not the Qc Oracle listener.";
    return;
  }
  banner.className = "banner ok";
  banner.textContent = `Live extract from ${payload.meta.table} for office ${payload.meta.office}.`;
}

function kpis(payload) {
  const k = payload.kpis;
  const cards = [
    ["Claims", whole.format(k.claims), "staging rows for this office"],
    ["Policies", whole.format(k.distinct_policies), `${k.claims_per_policy} claims / policy`],
    ["Repeat policies", whole.format(k.repeat_policies), `${k.repeat_share_pct}% of policies`],
    ["Claim IDs", whole.format(k.distinct_claim_ids), "distinct mapped claim numbers"],
    ["Total amount", fmtMoney(k.total_amount), k.amount_rows ? `avg ${fmtMoney(k.avg_amount)}` : "no amount column"],
    ["Approved", fmtMoney(k.approved_amount), "mapped approved amount"],
    ["Paid", fmtMoney(k.paid_amount), "mapped paid amount"],
    ["Date span", k.date_from ? `${k.date_from} → ${k.date_to}` : "—", "mapped claim date"],
  ];
  $("kpis").innerHTML = cards
    .map(
      ([label, value, foot]) =>
        `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="foot">${foot}</div></div>`
    )
    .join("");
}

function meta(payload) {
  const m = payload.meta;
  $("office-code").textContent = m.office || "771001";
  $("meta-box").innerHTML = [
    `<div><b>Table</b> ${m.table}</div>`,
    `<div><b>Source</b> ${m.source || "unknown"}</div>`,
    `<div><b>Generated</b> ${m.generated_at || ""}</div>`,
    `<div><b>Office rows</b> ${whole.format(m.scoped_rows || 0)} / ${whole.format(m.table_rows || 0)} in table</div>`,
    `<div><b>Filter</b> ${m.filter}</div>`,
  ].join("");
}

function bars(id, rows, nameKey, valueKey) {
  const el = $(id);
  hideIfEmpty(el, rows);
  if (!rows || !rows.length) return;
  const max = Math.max(...rows.map((row) => Number(row[valueKey]) || 0), 1);
  el.innerHTML = rows
    .map((row) => {
      const value = Number(row[valueKey]) || 0;
      const width = Math.max(2, Math.round((100 * value) / max));
      return `<div class="bar-row"><span>${row[nameKey]}</span><div class="track"><div class="fill" style="width:${width}%"></div></div><span class="n">${whole.format(value)}</span></div>`;
    })
    .join("");
}

function fillTable(id, rows) {
  const body = document.querySelector(`#${id} tbody`);
  if (!rows.length) {
    hideIfEmpty($(id), []);
    return;
  }
  body.innerHTML = rows
    .map(
      (row) => `<tr>
        <td>${row.policy}</td>
        <td>${row.dept || ""}</td>
        <td>${row.year || ""}</td>
        <td class="num">${whole.format(row.claims)}</td>
        <td class="num">${whole.format(row.distinct_claim_ids)}</td>
        <td class="num">${fmtMoney(row.amount)}</td>
      </tr>`
    )
    .join("");
}

function rank(id, rows, labelKey, valueKey) {
  const el = $(id);
  hideIfEmpty(el, rows);
  el.innerHTML = rows
    .map(
      (row) =>
        `<div class="row"><span>${row[labelKey]}</span><b>${whole.format(row[valueKey])}</b></div>`
    )
    .join("");
}

function render(payload) {
  setBanner(payload);
  meta(payload);
  kpis(payload);

  bars(
    "year-chart",
    (payload.by_year || []).map((row) => ({ name: row.year, count: row.claims })),
    "name",
    "count"
  );

  const yoy = payload.year_over_year || [];
  hideIfEmpty($("yoy"), yoy);
  $("yoy").innerHTML = yoy
    .map((row) => {
      const pct = row.pct == null ? "n/a" : `${row.pct}%`;
      return `<div class="row"><span>${row.from} → ${row.to}</span><b>${pct}</b></div>`;
    })
    .join("");

  bars(
    "month-chart",
    (payload.by_month || []).map((row) => ({ name: row.month, count: row.claims })),
    "name",
    "count"
  );
  bars("dept-chart", payload.by_dept || [], "name", "count");
  bars("status-chart", payload.by_status || [], "name", "count");
  bars("bucket-chart", payload.amount_buckets || [], "name", "count");
  fillTable("repeats-table", payload.repeats || []);
  rank("hospitals", payload.top_hospitals || [], "name", "count");
  rank("members", payload.member_repeats || [], "member", "claims");
  rank("tpas", payload.by_tpa || [], "name", "count");

  const q = payload.quality || {};
  $("quality").innerHTML = [
    ["Malformed policy nos", q.malformed_policy_numbers],
    ["Rows from other offices in this extract", q.rows_other_offices_seen],
    ["Single-claim policies", q.policies_single_claim],
    ["Max claims on one policy", q.max_claims_on_one_policy],
    ["Office share of table", q.office_share_pct != null ? `${q.office_share_pct}%` : "—"],
  ]
    .map(([label, value]) => `<div class="row"><span>${label}</span><b>${value ?? "—"}</b></div>`)
    .join("");

  const mapped = payload.meta.mapped || {};
  $("mapped").innerHTML = Object.keys(mapped).length
    ? Object.entries(mapped)
        .map(([role, col]) => `<span class="pill"><b>${role}</b> ${col}</span>`)
        .join("")
    : "<span class='hint'>No columns mapped — table was empty or unreachable.</span>";
  $("all-columns").textContent = (payload.meta.columns || []).join(", ") || "No column list.";
}

async function main() {
  try {
    const response = await fetch("data.json", { cache: "no-store" });
    if (!response.ok) throw new Error("data.json is missing. Run: python -m dumpdata dashboard");
    render(await response.json());
  } catch (err) {
    $("banner").className = "banner";
    $("banner").textContent = String(err.message || err);
    $("meta-box").textContent = "No extract loaded.";
  }
}

main();
