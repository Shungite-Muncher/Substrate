/* Substrate dashboard. Reads data/latest.json (published by the pipeline);
   chat and live part lookups go to the API (local server or Cloudflare Worker). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => DOMPurify.sanitize(marked.parse(s || ""));
const fmt = (n, d = 0) => (n == null || isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const isLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
const apiBase = () => (store.get("substrate.api") || window.SUBSTRATE_API || "").replace(/\/$/, "");
const apiReady = () => isLocal || !!apiBase();
const apiHeaders = () => ({ "content-type": "application/json", "x-access-code": store.get("substrate.code") || "" });

let DATA = null;
const charts = {};

// ---------- color + widgets -------------------------------------------------
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function riskColor(v) {
  if (v >= 62) return css("--risk-hi");
  if (v >= 54) return css("--risk-mid");
  if (v <= 46) return css("--risk-lo");
  return "#6b7280";
}
const badge = (v) => `<span class="badge" style="background:${riskColor(v)}">${fmt(v)}</span>`;
function meter(v) {
  return `<div class="meter"><i style="width:${Math.max(2, Math.min(100, v))}%;background:${riskColor(v)}"></i></div>`;
}
function normbar(n) {
  const w = Math.abs(n) * 50, left = n >= 0 ? 50 : 50 - w;
  return `<span class="normbar" title="${n.toFixed(2)}"><i style="left:${left}%;width:${w}%;background:${n >= 0 ? css("--pos") : css("--neg")}"></i></span>`;
}

// ---------- tabs --------------------------------------------------------------
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  store.set("substrate.tab", name);
  if (name === "market") drawMarketCharts();
  if (name === "outlook") drawOutlook();
  if (name === "peers") drawPeers();
  if (name === "backtest") drawBacktest();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- briefing ----------------------------------------------------------
function renderBriefing(i = 0) {
  const b = DATA.briefings[i];
  if (!b) { $("#briefing").innerHTML = `<p class="empty">No briefings yet. Run the pipeline.</p>`; return; }
  const when = new Date(b.created_at);
  $("#briefing").innerHTML = `<div class="edition"><span class="pill">${esc(b.edition)} edition</span>
    ${when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} · written by ${esc(b.model)}</div>` + md(b.body_md);
  $$("#briefing a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  $$("#archive li").forEach((li, j) => li.classList.toggle("active", j === i));
}
function renderArchive() {
  $("#archive").innerHTML = DATA.briefings.map((b) =>
    `<li>${new Date(b.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · ${esc(b.edition)}<br><small>${esc(b.headline)}</small></li>`).join("");
  $$("#archive li").forEach((li, i) => li.addEventListener("click", () => renderBriefing(i)));
  $("#headlines").innerHTML = DATA.news.slice(0, 12).map((n) =>
    `<li><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a><span>${esc(n.source)} · ${n.published ? new Date(n.published).toLocaleDateString() : ""}</span></li>`).join("");
}

// ---------- market ------------------------------------------------------------
function scoreCard(k, v, label, sub) {
  return `<div class="score"><div class="k">${k}</div><div class="v" style="color:${k === "Confidence" ? "inherit" : riskColor(v)}">${fmt(v)}</div>
    <div class="l">${esc(label)}</div><div class="hint" style="margin:0">${sub}</div>${k === "Confidence" ? `<div class="meter"><i style="width:${v}%;background:var(--accent)"></i></div>` : meter(v)}</div>`;
}
function renderMarket() {
  const m = DATA.market;
  $("#market-scores").innerHTML =
    scoreCard("Supply risk", m.supply_risk, m.labels.supply, "50 = neutral · higher = tighter supply") +
    scoreCard("Price trend", m.price_trend, m.labels.price, "50 = flat · higher = prices rising") +
    scoreCard("Confidence", m.confidence, m.labels.confidence,
      `agreement ${fmt(m.components.supply.agreement * 100)}% · coverage ${fmt(m.components.supply.coverage * 100)}%`);
  $("#segments").innerHTML = Object.values(DATA.segments).map((s) => `
    <div class="card seg" data-seg="${s.key}"><h4>${esc(s.label)}</h4>
      <div class="nums"><div><b style="color:${riskColor(s.supply_risk)}">${fmt(s.supply_risk)}</b><span>supply · ${esc(s.labels.supply)}</span></div>
      <div><b style="color:${riskColor(s.price_trend)}">${fmt(s.price_trend)}</b><span>price · ${esc(s.labels.price)}</span></div>
      <div><b>${fmt(s.confidence)}</b><span>confidence</span></div></div></div>`).join("");
  $$(".seg").forEach((el) => el.addEventListener("click", () => {
    $$(".seg").forEach((x) => x.classList.toggle("active", x === el));
    const s = DATA.segments[el.dataset.seg];
    const d = $("#segment-detail");
    d.hidden = false;
    d.innerHTML = `<div class="detail-head"><h3>${esc(s.label)}: what's driving the scores</h3>
      <button class="btn secondary" onclick="this.closest('.card').hidden=true">Close</button></div>` + signalTable(s.signals);
    d.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }));
}
function signalTable(sigs) {
  return `<div class="table-wrap" style="padding:0"><table class="sig-table"><thead><tr><th>Input</th><th>Score</th><th>Push</th><th class="num">Weight</th><th>Evidence</th></tr></thead><tbody>` +
    sigs.map((s) => `<tr><td><b>${esc(s.name)}</b><div class="src">${esc(s.source)}</div></td><td>${esc(s.component)}</td>
      <td>${normbar(s.norm)}</td><td class="num">${s.weight.toFixed(2)}</td>
      <td>${esc(s.evidence)} ${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">source</a>` : ""}</td></tr>`).join("") +
    `</tbody></table></div><p class="hint">Push: red = tighter supply / higher price (bad for buyers), green = easing. Score = 50 + 50 × weighted mean.</p>`;
}
function lineChart(id, pts, label, scale = 1) {
  const el = document.getElementById(id);
  if (!el || !pts?.length) return;
  charts[id]?.destroy();
  charts[id] = new Chart(el, {
    type: "line",
    data: { labels: pts.map((p) => p[0]), datasets: [{ label, data: pts.map((p) => p[1] / scale), borderColor: css("--accent"), backgroundColor: "transparent", pointRadius: 0, borderWidth: 2, tension: .25 }] },
    options: { animation: false, plugins: { legend: { display: false } }, interaction: { intersect: false, mode: "index" },
      scales: { x: { ticks: { maxTicksLimit: 8, color: css("--muted") }, grid: { display: false } }, y: { ticks: { color: css("--muted") }, grid: { color: css("--line") } } } },
  });
}
function drawMarketCharts() {
  const s = DATA.series;
  lineChart("c-tsmc", s.tsmc_revenue, "TSMC revenue", 1000);
  lineChart("c-sia", s.sia_sales, "Global sales");
  lineChart("c-cu", s.capacity_utilization, "Utilization");
  lineChart("c-ppi", s.ppi_semis, "PPI");
}

// ---------- BOM ---------------------------------------------------------------
let bomSort = { key: "supply_risk", dir: -1 };
function renderBom(parts = DATA.parts) {
  $("#bom-customer").textContent = `· ${DATA.customer}`;
  const cols = [
    ["mpn", "Part"], ["segment", "Segment"], ["supply_risk", "Supply risk"], ["price_trend", "Price trend"],
    ["confidence", "Conf."], ["lead_days", "Lead (wk)"], ["total_avail", "Stock"], ["price_1k", "1k price"], ["price_vs_target_pct", "vs target"],
  ];
  const sorted = [...parts].sort((a, b) => ((a[bomSort.key] ?? -1e9) > (b[bomSort.key] ?? -1e9) ? 1 : -1) * bomSort.dir);
  $("#bom-table").innerHTML = `<thead><tr>${cols.map(([k, l]) => `<th data-k="${k}">${l}${bomSort.key === k ? (bomSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>` +
    sorted.map((p) => `<tr data-mpn="${esc(p.mpn)}">
      <td><span class="mono">${esc(p.mpn)}</span><div class="src">${esc(p.manufacturer || "")}</div></td>
      <td>${esc(p.segment || "–")}</td><td>${badge(p.supply_risk)} <small>${esc(p.labels.supply)}</small></td>
      <td>${badge(p.price_trend)} <small>${esc(p.labels.price)}</small></td><td class="num">${fmt(p.confidence)}</td>
      <td class="num">${p.lead_days ? fmt(p.lead_days / 7) : "–"}</td><td class="num">${fmt(p.total_avail)}</td>
      <td class="num">${p.price_1k ? "$" + fmt(p.price_1k, 3) : "–"}</td>
      <td class="num" style="color:${(p.price_vs_target_pct || 0) > 0 ? css("--pos") : css("--neg")}">${p.price_vs_target_pct != null ? (p.price_vs_target_pct > 0 ? "+" : "") + fmt(p.price_vs_target_pct, 1) + "%" : "–"}</td></tr>`).join("") + "</tbody>";
  $$("#bom-table th").forEach((th) => th.addEventListener("click", () => {
    bomSort = { key: th.dataset.k, dir: bomSort.key === th.dataset.k ? -bomSort.dir : -1 };
    renderBom(parts);
  }));
  $$("#bom-table tbody tr").forEach((tr) => tr.addEventListener("click", () => {
    const p = parts.find((x) => x.mpn === tr.dataset.mpn);
    if (p) renderPartDetail(p, $("#part-detail"));
  }));
  if (!DATA.providers.length) {
    $("#bom-table").insertAdjacentHTML("afterend", `<p class="hint" id="no-prov">No part-data provider is configured yet, so part scores use segment signals only. Add Octopart (Nexar) or free Mouser/DigiKey keys to fill in lead times, stock and pricing.</p>`);
  }
}

// Scenario model: same formulas as substrate/signals.py part_signals()
function scenarioScore(p, leadWeeks, stock) {
  const sup = p.signals.filter((s) => s.component === "supply" && !["Factory lead time", "Channel inventory cover"].includes(s.name));
  const extra = [];
  const base = DATA.model?.baseline_lead_days ?? 84;
  if (leadWeeks != null) extra.push({ w: 0.30, n: Math.tanh((leadWeeks * 7 - base) / 60) });
  if (stock != null) {
    const n = p.annual_qty ? Math.tanh((8 - stock / (p.annual_qty / 52)) / 8) : Math.tanh((4 - Math.log10(stock + 1)) / 1.5);
    extra.push({ w: 0.20, n });
  }
  const all = sup.map((s) => ({ w: s.weight, n: s.norm })).concat(extra);
  const ws = all.reduce((a, x) => a + x.w, 0);
  return ws ? 50 + 50 * all.reduce((a, x) => a + x.w * x.n, 0) / ws : 50;
}

function renderPartDetail(p, host) {
  host.hidden = false;
  const lw = p.lead_days ? Math.round(p.lead_days / 7) : 12;
  const st = p.total_avail ?? (p.annual_qty ? Math.round(p.annual_qty / 52 * 8) : 10000);
  const stMax = Math.max(st * 4, p.annual_qty ? p.annual_qty / 2 : 50000);
  host.innerHTML = `<div class="detail-head"><div><h3 class="mono">${esc(p.mpn)}</h3>
      <div class="hint" style="margin:0">${esc(p.manufacturer || "")} · ${esc(p.description || "")} · ${esc(p.segment_label || p.segment || "unclassified")}</div></div>
      <div style="display:flex;gap:.5rem">${p.url ? `<a class="btn secondary" href="${esc(p.url)}" target="_blank" rel="noopener">Source listing</a>` : ""}
      <button class="btn" data-brief>Draft negotiation brief</button></div></div>
    <div class="kv">
      <div><span>Supply risk</span><b style="color:${riskColor(p.supply_risk)}">${fmt(p.supply_risk)} · ${esc(p.labels.supply)}</b></div>
      <div><span>Price trend</span><b style="color:${riskColor(p.price_trend)}">${fmt(p.price_trend)} · ${esc(p.labels.price)}</b></div>
      <div><span>Confidence</span><b>${fmt(p.confidence)} · ${esc(p.labels.confidence)}</b></div>
      <div><span>Factory lead time</span><b>${p.lead_days ? fmt(p.lead_days / 7) + " weeks" : "–"}</b></div>
      <div><span>Channel stock</span><b>${fmt(p.total_avail)}</b></div>
      <div><span>1k price / target</span><b>${p.price_1k ? "$" + fmt(p.price_1k, 3) : "–"} / ${p.target_price ? "$" + fmt(p.target_price, 2) : "–"}</b></div>
      <div><span>Annual usage</span><b>${fmt(p.annual_qty)}</b></div>
      <div><span>Data</span><b>${esc(p.provider || "none")} ${esc(p.data_day || "")}</b></div>
    </div>
    ${p.outlook ? `<div class="scenario"><b>3-month outlook</b> ${actBadge(p.outlook.action)}
      <div style="margin-top:.4rem">${pct(p.outlook.p_price_up)} chance prices rise · ${pct(p.outlook.p_supply_tighter)} chance supply tightens${
        p.outlook.lead_projection ? ` · lead time ${fmt(p.outlook.lead_projection.now_weeks)} → ${fmt(p.outlook.lead_projection.in_13w_weeks)} wk projected` : ""}</div>
      <small>${esc(p.outlook.why)}</small></div>` : ""}
    <div class="scenario"><b>Scenario model</b> <small>What would supply risk be if…</small>
      <label>Lead time <input type="range" min="2" max="60" value="${lw}" data-lt><span data-ltv>${lw} wk</span></label>
      <label>Channel stock <input type="range" min="0" max="${Math.round(stMax)}" step="${Math.max(1, Math.round(stMax / 200))}" value="${st}" data-st><span data-stv>${fmt(st)}</span></label>
      <div>Scenario supply risk: <span class="out" data-out></span> <small data-delta></small></div>
    </div>
    ${signalTable(p.signals)}
    ${(p.sellers || []).length ? `<h3 style="margin-top:1rem">Authorized sellers</h3><div class="table-wrap" style="padding:0"><table class="sig-table"><thead><tr><th>Seller</th><th class="num">Stock</th><th class="num">Lead (wk)</th><th class="num">MOQ</th><th class="num">1k price</th></tr></thead><tbody>` +
      p.sellers.map((s) => `<tr><td>${esc(s.seller)}</td><td class="num">${fmt(s.stock)}</td><td class="num">${s.lead_days ? fmt(s.lead_days / 7) : "–"}</td><td class="num">${fmt(s.moq)}</td><td class="num">${s.price_1k ? "$" + fmt(s.price_1k, 3) : "–"}</td></tr>`).join("") + "</tbody></table></div>" : ""}`;
  const upd = () => {
    const l = +$("[data-lt]", host).value, s = +$("[data-st]", host).value;
    $("[data-ltv]", host).textContent = `${l} wk`;
    $("[data-stv]", host).textContent = fmt(s);
    const v = scenarioScore(p, l, s);
    $("[data-out]", host).textContent = fmt(v);
    $("[data-out]", host).style.color = riskColor(v);
    const d = v - p.supply_risk;
    $("[data-delta]", host).textContent = `(${d >= 0 ? "+" : ""}${fmt(d, 1)} vs today)`;
  };
  $("[data-lt]", host).addEventListener("input", upd);
  $("[data-st]", host).addEventListener("input", upd);
  upd();
  $("[data-brief]", host).addEventListener("click", () => {
    showTab("chat");
    sendChat(`Prepare a negotiation brief for ${p.mpn} for my next supplier meeting.`, p.mpn);
  });
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// client-side BOM preview (persisting a BOM runs through the pipeline CLI)
$("#bom-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const rows = parseCsv(await f.text());
  const hdr = Object.keys(rows[0] || {});
  const find = (...names) => hdr.find((h) => names.includes(h.toLowerCase().replace(/[^a-z0-9]/g, "")));
  const mpnCol = find("mpn", "partnumber", "part", "manufacturerpartnumber", "mfrpartnumber", "pn");
  if (!mpnCol) { alert("No part-number column found."); return; }
  const segCol = find("segment", "commodity", "category");
  const qtyCol = find("annualqty", "annualusage", "eau", "qty", "quantity");
  const known = Object.fromEntries(DATA.parts.map((p) => [p.mpn, p]));
  const parts = rows.map((r) => {
    const mpn = (r[mpnCol] || "").replace(/\s+/g, "").toUpperCase();
    if (known[mpn]) return known[mpn];
    const seg = DATA.segments[(r[segCol] || "").toLowerCase()];
    const base = seg || DATA.market;
    return { mpn, segment: seg ? seg.key : null, segment_label: seg?.label, supply_risk: base.supply_risk, price_trend: base.price_trend,
      confidence: base.confidence, labels: base.labels, signals: base.signals, annual_qty: +r[qtyCol] || null };
  }).filter((p) => p.mpn);
  renderBom(parts);
  $("#no-prov")?.remove();
  $("#bom-table").insertAdjacentHTML("afterend", `<p class="hint" id="no-prov">Previewing ${parts.length} parts from ${esc(f.name)} with segment-level scores. To track them for real: <code>python -m substrate bom ${esc(f.name)} --refresh</code></p>`);
});
function parseCsv(text) {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/).filter(Boolean);
  const split = (l) => { const out = []; let cur = "", q = false;
    for (const ch of l) { if (ch === '"') q = !q; else if (ch === "," && !q) { out.push(cur); cur = ""; } else cur += ch; }
    out.push(cur); return out.map((s) => s.trim()); };
  const h = split(lines[0]);
  return lines.slice(1).map((l) => Object.fromEntries(split(l).map((v, i) => [h[i], v])));
}

// ---------- lookup ------------------------------------------------------------
$("#lookup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mpn = $("#lookup-mpn").value.replace(/\s+/g, "").toUpperCase();
  const host = $("#lookup-result");
  const known = DATA.parts.find((p) => p.mpn === mpn);
  if (known) { host.innerHTML = `<div class="card"></div>`; renderPartDetail(known, host.firstChild); return; }
  if (!apiReady()) { host.innerHTML = `<div class="card empty">${esc(mpn)} isn't in the BOM snapshot, and no live API is configured. Open "API settings" in the footer.</div>`; return; }
  host.innerHTML = `<div class="card empty">Looking up ${esc(mpn)}…</div>`;
  try {
    const r = await fetch(`${apiBase()}/api/part?mpn=${encodeURIComponent(mpn)}`, { headers: apiHeaders() });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    const s = j.snapshot || {};
    const p = { ...j, manufacturer: s.manufacturer, description: s.description, lead_days: s.lead_days, total_avail: s.total_avail,
      price_1k: s.price_1k, url: s.url, sellers: s.sellers, provider: s.provider, data_day: s.day };
    host.innerHTML = `<div class="card"></div>`;
    renderPartDetail(p, host.firstChild);
    if (!j.found) host.firstChild.insertAdjacentHTML("afterbegin", `<p class="hint">No live part data returned. Showing segment-level scores.</p>`);
  } catch (err) {
    host.innerHTML = `<div class="card empty">Lookup failed: ${esc(err.message)}</div>`;
  }
});

// ---------- chat ----------------------------------------------------------------
const chatHistory = [];
function addMsg(role, html, model) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = html + (model ? `<div class="model">${esc(model)}</div>` : "");
  $("#chat-log").appendChild(div);
  $("#chat-log").scrollTop = 1e9;
  return div;
}
async function sendChat(text, mpn) {
  if (!text.trim()) return;
  addMsg("user", esc(text));
  chatHistory.push({ role: "user", content: text });
  if (!apiReady()) {
    addMsg("bot", "The chat needs the API. Run <code>python -m substrate serve</code> locally, or set the Worker URL under API settings.");
    return;
  }
  const pending = addMsg("bot", "<em>Thinking…</em>");
  $("#chat-form button").disabled = true;
  try {
    const guess = mpn || DATA.parts.map((p) => p.mpn).find((m) => text.toUpperCase().includes(m));
    const r = await fetch(`${apiBase()}/api/chat`, { method: "POST", headers: apiHeaders(), body: JSON.stringify({ messages: chatHistory, mpn: guess }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    pending.innerHTML = md(j.reply) + `<div class="model">${esc(j.model)}</div>`;
    chatHistory.push({ role: "assistant", content: j.reply });
  } catch (err) {
    pending.innerHTML = `Sorry, the request failed: ${esc(err.message)}`;
  } finally {
    $("#chat-form button").disabled = false;
  }
}
$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#chat-input").value; $("#chat-input").value = ""; sendChat(t); });
$("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); } });
function renderChips() {
  const top = [...DATA.parts].sort((a, b) => b.supply_risk - a.supply_risk)[0];
  const seg = Object.values(DATA.segments).sort((a, b) => b.supply_risk - a.supply_risk)[0];
  const chips = [
    top && `Negotiation brief for ${top.mpn}`,
    seg && `Where is ${seg.label.toLowerCase()} heading next quarter, and why?`,
    `What if memory lead times stretch to 30 weeks?`,
    `Summarize today's briefing for my VP in 3 bullets`,
  ].filter(Boolean);
  $("#chat-chips").innerHTML = chips.map((c) => `<button>${esc(c)}</button>`).join("");
  $$("#chat-chips button").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
  $("#chat-status").textContent = apiReady() ? "" : "Chat API not configured. See API settings in the footer.";
}

// ---------- peers ----------------------------------------------------------------
function drawPeers() {
  const inv = DATA.inventory_days || {};
  const series = Object.entries(inv).filter(([, v]) => v.days.length);
  if (!series.length) {
    $("#inv-empty").textContent = "No SEC data yet. Set SEC_CONTACT_EMAIL (required by SEC's fair-access policy) and run the pipeline.";
    return;
  }
  const labels = [...new Set(series.flatMap(([, v]) => v.days.map((d) => d[0])))].sort();
  const palette = ["#0f766e", "#2563eb", "#9333ea", "#ea580c", "#65a30d", "#db2777", "#0891b2", "#ca8a04", "#4b5563", "#dc2626", "#7c3aed"];
  charts.inv?.destroy();
  charts.inv = new Chart($("#c-inv"), {
    type: "line",
    data: { labels, datasets: series.map(([t, v], i) => {
      const m = Object.fromEntries(v.days);
      const peer = DATA.peers.includes(t);
      return { label: `${v.name}${peer ? " (peer)" : ""}`, data: labels.map((l) => m[l] ?? null), borderColor: palette[i % palette.length],
        borderDash: peer ? [6, 4] : [], borderWidth: peer ? 3 : 1.8, pointRadius: 0, spanGaps: true, tension: .2 };
    }) },
    options: { animation: false, interaction: { intersect: false, mode: "index" }, plugins: { legend: { labels: { color: css("--ink"), boxWidth: 14 } } },
      scales: { x: { ticks: { color: css("--muted") }, grid: { display: false } }, y: { title: { display: true, text: "days", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { color: css("--line") } } } },
  });
}

// ---------- backtest -------------------------------------------------------------
let BT = null;
async function drawBacktest() {
  const b = DATA.backtest;
  const host = $("#backtest");
  if (!b) { host.innerHTML = `<p class="empty">Backtest runs with the pipeline.</p>`; return; }
  const p = b.price, pct = (x) => (x == null ? "–" : fmt(x * 100) + "%");
  host.innerHTML = `<h2 class="section" style="margin-top:0">Does the price score call direction?</h2>
    <p class="hint">${esc(p.target)}. Walk-forward from ${esc(p.window?.[0])} using only data published at each point (lags applied). Baseline = "the last 3 months continue".</p>
    <div class="table-wrap" style="padding:0"><table class="sig-table"><thead><tr><th>Model</th><th class="num">Hit rate</th><th class="num">Baseline</th><th class="num">Correlation</th><th class="num">Months</th></tr></thead><tbody>
    ${[["Base weights", p.uncalibrated], ["Calibrated (in-sample)", p.calibrated_in_sample], [`Calibrated out-of-sample (trained to ${esc(p.calibrated_out_of_sample.trained_through)})`, p.calibrated_out_of_sample]]
      .map(([n, r]) => `<tr><td>${n}</td><td class="num"><b>${pct(r.hit_rate)}</b></td><td class="num">${pct(r.baseline_hit_rate)}</td><td class="num">${fmt(r.correlation, 2)}</td><td class="num">${r.directional_months}</td></tr>`).join("")}
    </tbody></table></div>
    <p class="hint">Macro-only scoring roughly matches naive persistence. The edge we're validating comes from part-level lead-time and pricing history, which the accumulator is building. Lead-time backtest: <b>${esc(b.lead_time.status)}</b> (${b.lead_time.pairs} scored pairs so far${b.lead_time.hit_rate != null ? `, hit rate ${pct(b.lead_time.hit_rate)}` : ""}).</p>
    <p class="hint">Calibration weight factors: ${Object.entries(b.calibration.price_weight_factors).map(([k, v]) => `${esc(k)} ×${v}`).join(", ")}</p>`;
  if (!BT) { try { BT = await (await fetch("data/backtest.json", { cache: "no-store" })).json(); } catch { return; } }
  const s = BT.series.filter((r) => r.score != null);
  charts.bt?.destroy();
  charts.bt = new Chart($("#c-bt"), {
    data: { labels: s.map((r) => r.as_of.slice(0, 7)), datasets: [
      { type: "line", label: "Price score − 50", data: s.map((r) => r.score - 50), borderColor: css("--accent"), pointRadius: 0, borderWidth: 2, yAxisID: "y" },
      { type: "bar", label: "Next 3-mo PPI change %", data: s.map((r) => r.target), backgroundColor: s.map((r) => (r.target ?? 0) > 0 ? css("--pos") : css("--neg")), yAxisID: "y1" },
    ] },
    options: { animation: false, interaction: { intersect: false, mode: "index" }, plugins: { legend: { labels: { color: css("--ink") } } },
      scales: { x: { ticks: { maxTicksLimit: 10, color: css("--muted") }, grid: { display: false } },
        y: { position: "left", ticks: { color: css("--muted") }, grid: { color: css("--line") } }, y1: { position: "right", ticks: { color: css("--muted") }, grid: { display: false } } } },
  });
}

// ---------- outlook (predictive layer) --------------------------------------------
const ACTION_COLOR = { "Buy ahead / lock pricing": "--risk-hi", "Wait / keep terms short": "--risk-lo", "Hold course": null };
const actBadge = (a) => `<span class="act" style="background:${ACTION_COLOR[a] ? css(ACTION_COLOR[a]) : "#6b7280"}">${esc(a)}</span>`;
const pct = (x, d = 0) => (x == null ? "–" : fmt(x * 100, d) + "%");
const signed = (x, d = 1) => (x == null ? "–" : (x > 0 ? "+" : "") + fmt(x, d));
let FC = null, trTarget = "ppi_3m";

function renderOutlook() {
  const o = DATA.outlook;
  if (!o) { $("#forecast-cards").innerHTML = `<div class="card empty">No forecasts yet. They run with the pipeline.</div>`; return; }
  const any = Object.values(o.forecasts)[0];
  $("#outlook-asof").textContent = any ? `· data through ${any.base_period}` : "";
  $("#forecast-cards").innerHTML = Object.entries(o.forecasts).map(([k, f]) => {
    const m = f.metrics, edge = m.verdict === "edge";
    const pColor = riskColor(50 + (f.prob_up - 0.5) * 100);
    const range = f.lo == null ? "" : ` <small>(80% range ${signed(f.lo)} to ${signed(f.hi)}${esc(f.unit)})</small>`;
    return `<div class="card fc">
      <h3>${esc(f.label)}</h3>
      <div class="p" style="color:${pColor}">${pct(f.prob_up)} <small>chance ${esc(f.up)}</small></div>
      <div class="exp">Expected ${signed(f.point)}${esc(f.unit)}${range}</div>
      <span class="tag ${edge ? "edge" : "base"}">${edge ? "Tested edge over trend-following" : "Trend-following baseline: no tested edge yet"}</span>
      <div class="stats">Out of sample ${esc(m.window?.[0] || "")}–${esc(m.window?.[1] || "")} (${m.n} months):
        direction right ${pct(m.hit_rate_model)} (trend ${pct(m.hit_rate_trend)}), probability skill vs trend ${signed((m.skill_vs_trend || 0) * 100)}%,
        80% range held ${pct(m.interval_coverage_80)}.</div>
      <details><summary>What's driving the model</summary><div class="drv">${f.drivers.map((d) =>
        `<span>${esc(d.label)} <small>(${fmt(d.value, 1)})</small></span>${normbar(Math.max(-1, Math.min(1, d.logodds / 2)))}`).join("")}</div>
        <p class="hint" style="margin:.4rem 0 0">Model probability ${pct(f.prob_model)} · trend-following ${pct(f.prob_trend)} · showing the ${esc(f.basis)}.</p></details>
    </div>`;
  }).join("");

  const parts = [...DATA.parts].filter((p) => p.outlook).sort((a, b) => b.outlook.p_price_up - a.outlook.p_price_up);
  $("#timing-table").innerHTML = `<thead><tr><th>Part</th><th>Call</th><th class="num">P(price up, 3 mo)</th><th class="num">P(supply tighter)</th><th class="num">Lead now → in 13 wk</th><th>Why</th></tr></thead><tbody>` +
    parts.map((p) => {
      const q = p.outlook, lp = q.lead_projection;
      return `<tr><td class="mono">${esc(p.mpn)}</td><td>${actBadge(q.action)}</td><td class="num">${pct(q.p_price_up)}</td><td class="num">${pct(q.p_supply_tighter)}</td>
        <td class="num">${lp ? `${fmt(lp.now_weeks)} → <b>${fmt(lp.in_13w_weeks)}</b> wk` : "<small>needs part data</small>"}</td><td>${esc(q.why)}</td></tr>`;
    }).join("") + "</tbody>";

  $("#seg-outlook").innerHTML = `<thead><tr><th>Segment</th><th>Supply, 3 mo</th><th class="num">P(tighter)</th><th>Prices, 3 mo</th><th class="num">P(up)</th></tr></thead><tbody>` +
    Object.values(o.segments || {}).map((s) => `<tr><td>${esc(s.label)}</td><td>${esc(s.supply_call)}</td><td class="num">${pct(s.p_supply_tighter)}</td>
      <td>${esc(s.price_call)}</td><td class="num">${pct(s.p_price_up)}</td></tr>`).join("") + "</tbody>";

  const L = o.ledger;
  $("#ledger").innerHTML = `<div class="ledger-stats"><div><b>${L.made}</b><span>forecasts logged</span></div><div><b>${L.resolved}</b><span>scored so far</span></div>
    <div><b>${L.hit_rate == null ? "–" : pct(L.hit_rate)}</b><span>live direction hit rate</span></div></div>
    <p class="hint" style="margin:0">Every forecast is written down the first time it's made, then scored when the outcome is published. Revisions aren't allowed. Market forecasts resolve when the Fed/BLS data for their target month lands; part lead-time projections after 13 weeks. This live record is the validation milestone.</p>`;
  $("#outlook-notes").textContent = Object.values(o.notes || {}).join(" ");
}

async function drawOutlook() {
  const o = DATA.outlook;
  if (!o) return;
  const hist = DATA.series.ppi_semis.slice(-36);
  const f3 = o.forecasts.ppi_3m, f6 = o.forecasts.ppi_6m;
  if (hist.length && f3) {
    const baseIdx = hist.findIndex((h) => h[0] === f3.base_period);
    const keep = baseIdx >= 0 ? hist.slice(0, baseIdx + 1) : hist;
    const base = keep[keep.length - 1][1];
    const lvl = (x) => (x == null ? null : base * Math.exp(x / 100));
    const labels = keep.map((h) => h[0]).concat([f3.target_period, f6 ? f6.target_period : null].filter(Boolean));
    const pad = (arr) => Array(keep.length - 1).fill(null).concat(arr);
    const tail = (k) => [base, lvl(f3[k])].concat(f6 ? [lvl(f6[k])] : []);
    charts.fan?.destroy();
    charts.fan = new Chart($("#c-fan"), {
      type: "line",
      data: { labels, datasets: [
        { label: "PPI", data: keep.map((h) => h[1]), borderColor: css("--accent"), pointRadius: 0, borderWidth: 2 },
        { label: "80% low", data: pad(tail("lo")), borderColor: "transparent", pointRadius: 0, fill: false },
        { label: "80% high", data: pad(tail("hi")), borderColor: "transparent", pointRadius: 0, fill: "-1", backgroundColor: "rgba(180,83,9,.18)" },
        { label: "Forecast", data: pad(tail("point")), borderColor: css("--risk-mid"), borderDash: [6, 4], pointRadius: 3, borderWidth: 2 },
      ] },
      options: { animation: false, interaction: { intersect: false, mode: "index" },
        plugins: { legend: { display: false } },
        scales: { x: { ticks: { maxTicksLimit: 8, color: css("--muted") }, grid: { display: false } }, y: { ticks: { color: css("--muted") }, grid: { color: css("--line") } } } },
    });
  }
  if (!FC) { try { FC = await (await fetch("data/forecasts.json", { cache: "no-store" })).json(); } catch { return; } }
  $("#tr-pick").innerHTML = Object.entries(FC.forecasts).map(([k, f]) =>
    `<button data-k="${k}" class="${k === trTarget ? "on" : ""}">${esc(o.forecasts[k]?.short || f.label)}</button>`).join("");
  $$("#tr-pick button").forEach((b) => b.addEventListener("click", () => { trTarget = b.dataset.k; drawOutlook(); }));
  const h = FC.forecasts[trTarget].history;
  $("#tr-label").textContent = `· ${FC.forecasts[trTarget].label}`;
  charts.track?.destroy();
  charts.track = new Chart($("#c-track"), {
    data: { labels: h.map((r) => r.period), datasets: [
      { type: "line", label: "Model P(up)", data: h.map((r) => r.p_model * 100), borderColor: css("--accent"), pointRadius: 0, borderWidth: 2, yAxisID: "y" },
      { type: "line", label: "Trend-following P(up)", data: h.map((r) => r.p_trend * 100), borderColor: css("--muted"), borderDash: [4, 3], pointRadius: 0, borderWidth: 1.5, yAxisID: "y" },
      { type: "bar", label: `Actual change (${FC.forecasts[trTarget].unit})`, data: h.map((r) => r.y), backgroundColor: h.map((r) => (r.y > 0 ? css("--pos") : css("--neg"))), yAxisID: "y1" },
    ] },
    options: { animation: false, interaction: { intersect: false, mode: "index" }, plugins: { legend: { labels: { color: css("--ink"), boxWidth: 12 } } },
      scales: { x: { ticks: { maxTicksLimit: 9, color: css("--muted") }, grid: { display: false } },
        y: { min: 0, max: 100, position: "left", ticks: { color: css("--muted"), callback: (v) => v + "%" }, grid: { color: css("--line") } },
        y1: { position: "right", ticks: { color: css("--muted") }, grid: { display: false } } } },
  });
}

// ---------- settings -------------------------------------------------------------
$("#settings-btn").addEventListener("click", () => {
  $("#set-api").value = store.get("substrate.api") || window.SUBSTRATE_API || "";
  $("#set-code").value = store.get("substrate.code") || "";
  $("#settings").showModal();
});
$("#set-save").addEventListener("click", () => {
  store.set("substrate.api", $("#set-api").value.trim());
  store.set("substrate.code", $("#set-code").value);
  renderChips();
});

// ---------- boot ------------------------------------------------------------------
(async function boot() {
  try {
    DATA = await (await fetch("data/latest.json", { cache: "no-store" })).json();
  } catch {
    $("main").innerHTML = `<div class="card empty">No data published yet. Run <code>python -m substrate run --backfill</code>.</div>`;
    return;
  }
  const asOf = new Date(DATA.generated);
  $("#meta").innerHTML = `<b>${esc(DATA.customer)}</b><br>updated ${asOf.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`;
  renderBriefing(0);
  renderArchive();
  renderMarket();
  renderOutlook();
  renderBom();
  renderChips();
  const params = new URLSearchParams(location.search);
  if (params.get("part")) {
    showTab("lookup");
    $("#lookup-mpn").value = params.get("part");
    $("#lookup-form").requestSubmit();
  } else {
    showTab(params.get("tab") || store.get("substrate.tab") || "briefing");
  }
})();
