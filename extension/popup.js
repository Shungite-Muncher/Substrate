const out = document.getElementById("out");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => (n == null ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const color = (v) => (v >= 62 ? "var(--hi)" : v >= 54 ? "var(--mid)" : v <= 46 ? "var(--lo)" : "var(--muted)");

async function run(mpn) {
  mpn = normalize(mpn);
  if (!mpn) return;
  document.getElementById("q").value = mpn;
  out.innerHTML = `<p class="muted">Looking up ${esc(mpn)}…</p>`;
  try {
    const p = await lookup(mpn);
    const s = p.snapshot || {};
    const { site } = await settings();
    out.innerHTML = `<div class="card">
      <div class="mpn">${esc(p.mpn || mpn)}</div>
      <div class="sub">${esc(s.manufacturer || "")} ${s.description ? "· " + esc(s.description) : ""} · ${esc(p.segment || "unclassified")}</div>
      <div class="scores">
        <div><b style="color:${color(p.supply_risk)}">${fmt(p.supply_risk)}</b><span>Supply risk<br>${esc(p.labels?.supply)}</span></div>
        <div><b style="color:${color(p.price_trend)}">${fmt(p.price_trend)}</b><span>Price trend<br>${esc(p.labels?.price)}</span></div>
        <div><b>${fmt(p.confidence)}</b><span>Confidence<br>${esc(p.labels?.confidence)}</span></div>
      </div>
      <div class="kv">
        <div><span>Lead time</span><br><b>${s.lead_days ? fmt(s.lead_days / 7) + " wk" : "–"}</b></div>
        <div><span>Channel stock</span><br><b>${fmt(s.total_avail)}</b></div>
        <div><span>1k price</span><br><b>${s.price_1k ? "$" + fmt(s.price_1k, 3) : "–"}</b></div>
        <div><span>Data</span><br><b>${esc(p.via)}</b></div>
      </div>
      <b style="font-size:12px">Top drivers</b>
      <ul>${(p.signals || []).slice(0, 3).map((x) => `<li>${esc(x.evidence)}</li>`).join("")}</ul>
      <a class="open" href="${esc(site)}/?part=${encodeURIComponent(mpn)}" target="_blank">Open full analysis &amp; negotiation brief →</a>
    </div>`;
  } catch (e) {
    out.innerHTML = `<div class="card"><b>${esc(mpn)}</b><p class="muted">${esc(e.message)}</p></div>`;
  }
}

document.getElementById("f").addEventListener("submit", (e) => { e.preventDefault(); run(document.getElementById("q").value); });

// If opened after a right-click lookup, show that part.
chrome.storage.session.get("pending").then(({ pending }) => {
  if (pending) { chrome.storage.session.remove("pending"); run(pending); }
});
