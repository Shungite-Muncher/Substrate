/**
 * Substrate API worker (Cloudflare Workers free tier).
 *
 *   GET  /api/health
 *   GET  /api/part?mpn=...   BOM snapshot -> edge cache -> Octopart (Nexar) -> Mouser
 *   POST /api/chat           { messages: [{role, content}], mpn? }
 *
 * Secrets (wrangler secret put ...): GROQ_API_KEY (or GEMINI_API_KEY), NEXAR_CLIENT_ID, NEXAR_CLIENT_SECRET,
 * MOUSER_API_KEY, ACCESS_CODE (optional but recommended; protects your free quotas).
 * Vars (wrangler.toml): SITE_URL, GEMINI_MODEL, NEXAR_MONTHLY_PART_BUDGET.
 * Optional KV binding BUDGET enforces the Nexar monthly budget across all edge locations.
 */

const SYSTEM = `You are Substrate, a negotiation-prep analyst for semiconductor procurement teams.
You have the customer's live market context (scores 0-100, 50 = neutral; supply risk higher = tighter;
price trend higher = rising) and their BOM. Rules:
- Ground every claim in the provided context. If the data doesn't cover something, say so plainly.
- Never claim knowledge of any company's confidential pricing or contract terms; position inferences come from public signals only.
- Be concrete: numbers, part numbers, what to ask the supplier for, and fallback positions.
- For negotiation briefs use sections: Situation, Leverage (ours / theirs), Asks, Walk-away & alternatives, Talking points.
- For scenario questions ("what if lead times hit 30 weeks"), reason explicitly from the scoring inputs.
- Glossary: PBO = on-order / backorder quantity: units the distributor has on order from the manufacturer, with the next expected arrival date. Lead time = factory lead time. Price = unit price at qty 1 and at 1k.
- When a live part lookup is provided, open with that part's lead time (in weeks), PBO (quantity and next arrival date), stock and current price (qty 1 and 1k), quoting the numbers exactly, then the brief.
- Keep answers under 350 words unless asked for more. Markdown.`;

const NEXAR_QUERY = `query SubstratePart($q: String!) {
  supSearchMpn(q: $q, limit: 1) { results { part {
    mpn manufacturer { name } shortDescription category { name path } octopartUrl totalAvail
    estimatedFactoryLeadDays medianPrice1000 { price currency }
    sellers(authorizedOnly: true) { company { name } offers { inventoryLevel factoryLeadDays onOrderQuantity moq prices { quantity price currency } } }
  } } }
}`;

const CATEGORY_HINTS = [
  [/memory|dram|sdram|flash|eeprom|sram|nand/i, "memory"],
  [/microcontroller|mcu|embedded - micro/i, "mcu"],
  [/mosfet|igbt|transistor|diode|rectifier|thyristor|discrete|sic|gan/i, "power"],
  [/regulator|pmic|power management|amplifier|op amp|data acquisition|adc|dac|analog|interface|driver|reference/i, "analog"],
  [/fpga|processor|cpu|gpu|dsp|soc|logic/i, "logic"],
];
const BASELINE_LEAD_DAYS = 84;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, x-access-code",
};
const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...cors, ...extra } });

// crude per-isolate limiter; the access code is the real protection
const hits = new Map();
function limited(ip, max = 30) {
  const now = Date.now(), win = 60_000;
  const h = (hits.get(ip) || []).filter((t) => now - t < win);
  h.push(now);
  hits.set(ip, h);
  return h.length > max;
}

async function siteJson(env, name, ctx) {
  const url = `${env.SITE_URL.replace(/\/$/, "")}/data/${name}`;
  const r = await fetch(url, { cf: { cacheTtl: 600, cacheEverything: true } });
  if (!r.ok) throw new Error(`could not load ${name} (${r.status})`);
  return r.json();
}

// ---------------- part data ----------------
const tanh = Math.tanh;
function priceAt(breaks, qty = 1000) {
  const b = breaks.filter(([q, p]) => q && p).sort((a, c) => a[0] - c[0]);
  if (!b.length) return null;
  const ok = b.filter(([q]) => q <= qty);
  return ok.length ? ok[ok.length - 1][1] : b[0][1];
}

let nexarToken = null;
async function nexarLookup(env, mpn) {
  if (!nexarToken || nexarToken.exp < Date.now() + 60_000) {
    const r = await fetch("https://identity.nexar.com/connect/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "client_credentials", client_id: env.NEXAR_CLIENT_ID, client_secret: env.NEXAR_CLIENT_SECRET }),
    });
    if (!r.ok) throw new Error(`nexar token ${r.status}`);
    const j = await r.json();
    nexarToken = { value: j.access_token, exp: Date.now() + (j.expires_in || 3600) * 1000 };
  }
  const ask = async (query) => {
    const r = await fetch("https://api.nexar.com/graphql/", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${nexarToken.value}` },
      body: JSON.stringify({ query, variables: { q: mpn } }),
    });
    // GraphQL validation errors arrive as HTTP 400 with the reason in the body
    try { return await r.json(); } catch { throw new Error(`nexar ${r.status}`); }
  };
  // Nexar's schema varies by plan: drop any field or argument it rejects and retry
  let query = NEXAR_QUERY, j = {};
  for (let i = 0; i < 5; i++) {
    j = await ask(query);
    const bad = [];
    for (const e of j.errors || []) {
      const m = String(e.message || "").match(/[`"](\w+)[`"] does not exist|Cannot query field [`"](\w+)[`"]/i);
      if (m) bad.push(m[1] || m[2]);
      const a = String(e.message || "").match(/argument [`"](\w+)[`"]/i);
      if (a) bad.push("(" + a[1]);
    }
    if (!bad.length) break;
    console.log("nexar rejected", bad.join(", "));
    for (const n of bad) {
      query = n.startsWith("(")
        ? query.replace(new RegExp("\\(\\s*" + n.slice(1) + "[^)]*\\)", "g"), "")
        : query.replace(new RegExp("\\b" + n + "\\b(\\s*\\([^()]*\\))?(\\s*\\{(?:[^{}]|\\{(?:[^{}]|\\{[^{}]*\\})*\\})*\\})?", "g"), "");
    }
  }
  let p = j?.data?.supSearchMpn?.results?.[0]?.part;
  if (!p && mpn.includes("#")) {        // e.g. LTC3780EGN#PBF: packaging suffixes often aren't indexed
    const r2 = await fetch("https://api.nexar.com/graphql/", { method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${nexarToken.value}` },
      body: JSON.stringify({ query, variables: { q: mpn.split("#")[0] } }) });
    p = (await r2.json().catch(() => ({})))?.data?.supSearchMpn?.results?.[0]?.part;
  }
  if (!p) return null;
  const sellers = [];
  for (const s of p.sellers || []) for (const o of s.offers || []) {
    sellers.push({ seller: s.company.name, stock: o.inventoryLevel, lead_days: o.factoryLeadDays, moq: o.moq,
      on_order: o.onOrderQuantity, on_order_eta: o.onOrderEta,
      price_1k: priceAt((o.prices || []).filter((x) => !x.currency || x.currency === "USD").map((x) => [x.quantity, x.price])) });
  }
  return { mpn: p.mpn || mpn, provider: "octopart", manufacturer: p.manufacturer?.name, description: p.shortDescription,
    category: p.category?.path || p.category?.name, total_avail: p.totalAvail, lead_days: p.estimatedFactoryLeadDays,
    price_1k: p.medianPrice1000?.price, url: p.octopartUrl, sellers: sellers.slice(0, 15), day: new Date().toISOString().slice(0, 10),
    price_1: (() => { const ones = (p.sellers || []).flatMap((s) => (s.offers || []).flatMap((o) => (o.prices || []).filter((x) => x.quantity === 1).map((x) => x.price))); return ones.length ? Math.min(...ones) : null; })(),
    on_order: sellers.reduce((a, s) => a + (s.on_order || 0), 0) || null,
    on_order_eta: sellers.map((s) => s.on_order_eta).filter(Boolean).sort()[0] || null };
}

async function mouserLookup(env, mpn) {
  const r = await fetch(`https://api.mouser.com/api/v1/search/partnumber?apiKey=${encodeURIComponent(env.MOUSER_API_KEY)}`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ SearchByPartRequest: { mouserPartNumber: mpn, partSearchOptions: "Exact" } }),
  });
  if (!r.ok) throw new Error(`mouser ${r.status}`);
  const parts = (await r.json())?.SearchResults?.Parts || [];
  const exact = parts.filter((x) => (x.ManufacturerPartNumber || "").toUpperCase() === mpn);
  // Mouser lists one MPN under several Mouser numbers, some empty: combine every exact match.
  const money = (s) => { const m = String(s || "").match(/[\d.,]+/); return m ? parseFloat(m[0].replace(/,/g, "")) : null; };
  const int = (v) => { const n = parseInt(String(v ?? "").replace(/,/g, ""), 10); return isNaN(n) ? null : n; };
  const pool = (exact.length ? exact : parts.slice(0, 1)).map((p) => {
    const lm = String(p.LeadTime || "").match(/(\d+)\s*(week|day)?/i);
    const lead = lm ? (+lm[1]) * (/week/i.test(lm[2] || "") ? 7 : 1) : null;
    const oo = (p.AvailabilityOnOrder || []).filter((x) => x && typeof x === "object");
    return { p, breaks: (p.PriceBreaks || []).map((b) => [+b.Quantity, money(b.Price)]), stock: int(p.AvailabilityInStock),
      lead: lead || null, onOrder: oo.reduce((a, x) => a + (int(x.Quantity) || 0), 0),
      etas: oo.map((x) => String(x.Date || "").slice(0, 10)).filter(Boolean) };
  });
  if (!pool.length) return null;
  const live = pool.filter((x) => x.breaks.length || x.stock);
  const best = (live.length ? live : pool).reduce((a, b) => ((b.stock || 0) > (a.stock || 0) ? b : a));
  const stocks = pool.map((x) => x.stock).filter((x) => x != null);
  const p1 = pool.filter((x) => x.breaks.length).map((x) => priceAt(x.breaks, 1));
  const pk = pool.filter((x) => x.breaks.length).map((x) => priceAt(x.breaks));
  const p = best.p;
  // origin, classification and tariff surcharge (Mouser's trade/compliance fields)
  const comp = (list) => Object.fromEntries((list || []).map((x) => [x.ComplianceName, x.ComplianceValue]));
  const clean = (v) => (!v || /^(not available|none)$/i.test(String(v).trim()) ? null : String(v).trim());
  const tc = comp(p.TradeCompliance), pc = comp(p.ProductCompliance);
  const notes = pool.flatMap((x) => (x.p.SurchargeMessages || []).map((m) => m && m.message).filter(Boolean));
  const pcts = notes.flatMap((n) => [...n.matchAll(/(\d+(?:\.\d+)?)\s*%/g)].map((m) => +m[1]));
  const trade = { country_of_origin: clean(tc["Country of Origin"]), fab_country: clean(tc["Country of Diffusion"]),
    assembly_country: clean(tc["Assembly Country of Origin"]), us_hts: clean(pc.USHTS), eccn: clean(pc.ECCN),
    tariff_note: notes.join(" ") || null, tariff_pct: pcts.length ? Math.max(...pcts) : null,
    notice: pool.flatMap((x) => x.p.InfoMessages || []).join(" ") || null };
  return { ...trade, mpn: p.ManufacturerPartNumber || mpn, provider: "mouser", manufacturer: p.Manufacturer, description: p.Description,
    category: p.Category, total_avail: stocks.length ? stocks.reduce((a, b) => a + b, 0) : null,
    lead_days: best.lead || (live.find((x) => x.lead) || {}).lead || null,
    price_1k: pk.length ? Math.min(...pk) : null, price_1: p1.length ? Math.min(...p1) : null,
    lifecycle: p.LifecycleStatus || (pool.find((x) => x.p.LifecycleStatus) || {}).p?.LifecycleStatus || null,
    on_order: pool.reduce((a, x) => a + x.onOrder, 0) || null, on_order_eta: pool.flatMap((x) => x.etas).sort()[0] || null,
    url: p.ProductDetailUrl,
    sellers: (live.length ? live : pool).map((x) => ({ seller: `Mouser (${x.p.MouserPartNumber || "listing"})`, stock: x.stock,
      lead_days: x.lead, on_order: x.onOrder || null, on_order_eta: x.etas.sort()[0] || null,
      price_1k: x.breaks.length ? priceAt(x.breaks) : null })),
    day: new Date().toISOString().slice(0, 10) };
}

async function nexarBudgetOk(env) {
  if (!env.BUDGET) return true;
  const key = `nexar:${new Date().toISOString().slice(0, 7)}`;
  const used = parseInt((await env.BUDGET.get(key)) || "0", 10);
  return used < parseInt(env.NEXAR_MONTHLY_PART_BUDGET || "40", 10);
}
async function nexarBudgetAdd(env) {
  if (!env.BUDGET) return;
  const key = `nexar:${new Date().toISOString().slice(0, 7)}`;
  const used = parseInt((await env.BUDGET.get(key)) || "0", 10);
  await env.BUDGET.put(key, String(used + 1), { expirationTtl: 60 * 60 * 24 * 40 });
}

async function liveLookup(env, mpn, ctx) {
  const cache = caches.default;
  const key = new Request(`https://substrate-cache.internal/v3/part/${encodeURIComponent(mpn)}`);
  const hit = await cache.match(key);
  if (hit) return hit.json();
  let snap = null;
  if (env.NEXAR_CLIENT_ID && env.NEXAR_CLIENT_SECRET && (await nexarBudgetOk(env))) {
    try { snap = await nexarLookup(env, mpn); if (snap) await nexarBudgetAdd(env); } catch (e) { console.log(e.message); }
  }
  if (!snap && env.MOUSER_API_KEY) {
    try { snap = await mouserLookup(env, mpn); } catch (e) { console.log(e.message); }
  }
  if (snap) {
    ctx.waitUntil(cache.put(key, new Response(JSON.stringify(snap), { headers: { "cache-control": "max-age=259200" } })));
  }
  return snap;
}

// Mirrors substrate/signals.py part_signals() + scoring.summarize() for parts outside the BOM.
function scorePart(latest, snap) {
  const seg = snap ? (CATEGORY_HINTS.find(([re]) => re.test(snap.category || snap.description || "")) || [])[1] : null;
  const base = (seg && latest.segments[seg]) || latest.market;
  const sigs = base.signals.map((s) => ({ ...s, weight: s.weight * 0.5 }));
  if (snap) {
    const src = snap.provider === "octopart" ? "Octopart" : "Part data";
    if (snap.lead_days != null) sigs.push({ name: "Factory lead time", source: src, component: "supply", weight: 0.30,
      norm: tanh((snap.lead_days - BASELINE_LEAD_DAYS) / 90), evidence: `Factory lead time ${Math.round(snap.lead_days / 7)} weeks vs a ~12-week norm (${snap.provider})`, url: snap.url });
    if (snap.total_avail != null) sigs.push({ name: "Channel inventory cover", source: src, component: "supply", weight: 0.20,
      norm: tanh((4 - Math.log10(snap.total_avail + 1)) / 1.5), evidence: `Channel stock ${snap.total_avail.toLocaleString()} units across authorized sellers`, url: snap.url });
    const n = new Set((snap.sellers || []).filter((s) => (s.stock || 0) > 0).map((s) => s.seller)).size;
    if ((snap.sellers || []).length) sigs.push({ name: "Sources with stock", source: src, component: "supply", weight: 0.05,
      norm: tanh((3 - n) / 2), evidence: `${n} authorized seller(s) currently holding stock`, url: snap.url });
    if (snap.tariff_pct) sigs.push({ name: "Tariff surcharge", source: src, component: "price", weight: 0.20,
      norm: tanh(snap.tariff_pct / 30), evidence: `${snap.tariff_note} (origin: ${snap.country_of_origin || "unknown"})`, url: snap.url });
    if (/nrnd|not recommended|obsolete|eol|end of life|last time/i.test(snap.lifecycle || "")) sigs.push({ name: "Lifecycle status",
      source: src, component: "supply", weight: 0.15, norm: 1, evidence: `Lifecycle: ${snap.lifecycle}`, url: snap.url });
  }
  const comp = (c) => {
    const xs = sigs.filter((s) => s.component === c && s.weight > 0);
    const w = xs.reduce((a, s) => a + s.weight, 0);
    return w ? 50 + 50 * xs.reduce((a, s) => a + s.weight * s.norm, 0) / w : 50;
  };
  const supply = +comp("supply").toFixed(1), price = +comp("price").toFixed(1);
  const lab = (s, kind) => kind === "supply"
    ? (s >= 85 ? "Severe" : s >= 70 ? "Elevated" : s >= 58 ? "Rising" : s > 42 ? "Neutral" : s > 30 ? "Easing" : "Low")
    : (s >= 85 ? "Rising fast" : s >= 68 ? "Rising" : s >= 57 ? "Firming" : s > 43 ? "Flat" : s > 32 ? "Softening" : "Falling");
  const conf = base.confidence * (snap ? 1 : 0.8);
  sigs.sort((a, b) => Math.abs(b.weight * b.norm) - Math.abs(a.weight * a.norm));
  return { segment: seg, supply_risk: supply, price_trend: price, confidence: +conf.toFixed(1),
    labels: { supply: lab(supply, "supply"), price: lab(price, "price"), confidence: conf >= 70 ? "High" : conf >= 50 ? "Medium" : "Low" },
    signals: sigs.slice(0, 10) };
}

async function handlePart(env, url, ctx) {
  const mpn = (url.searchParams.get("mpn") || "").replace(/\s+/g, "").toUpperCase();
  if (!mpn || mpn.length > 60) return json({ error: "mpn required" }, 400);
  const parts = await siteJson(env, "parts.json", ctx);
  if (parts[mpn]) {
    const p = parts[mpn];
    return json({ mpn, found: true, segment: p.segment, supply_risk: p.supply_risk, price_trend: p.price_trend, confidence: p.confidence,
      labels: p.labels, signals: p.signals, snapshot: { ...p, day: p.data_day }, source: "bom-snapshot" });
  }
  const latest = await siteJson(env, "latest.json", ctx);
  const snap = await liveLookup(env, mpn, ctx);
  return json({ mpn, found: !!snap, ...scorePart(latest, snap), snapshot: snap, source: snap ? snap.provider : "segment-only" });
}

// ---------------- chat ----------------
// Groq free tier: 30 req/min, 1K req/day, 8K tokens/min, so prompts stay compact.
async function groq(env, prompt) {
  const model = env.GROQ_MODEL || "openai/gpt-oss-120b";
  const body = { model, temperature: 0.4, max_completion_tokens: 1100,
    messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt }] };
  if (model.includes("gpt-oss")) Object.assign(body, { reasoning_effort: "low", include_reasoning: false });
  const r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
    method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${env.GROQ_API_KEY}` },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j?.error?.message || `groq ${r.status}`);
  const text = j.choices?.[0]?.message?.content || "";
  if (!text.trim()) throw new Error("empty response");
  return { reply: text, model: `groq/${model}` };
}

function compactContext(ctx, question) {
  const q = question.toUpperCase();
  const c = { ...ctx, latest_briefing: (ctx.latest_briefing || "").split("**Sources**")[0].slice(0, 2000) };
  for (let level = 0; level < 4; level++) {
    let bom = ctx.bom || [];
    if (level >= 1) bom = bom.map((p) => (q.includes(p.mpn) ? p : { mpn: p.mpn, segment: p.segment, supply_risk: p.supply_risk, price_trend: p.price_trend }));
    if (level >= 2) { c.headlines = (ctx.headlines || []).slice(0, 6).map((h) => h.title); delete c.peer_inventory_days; }
    if (level >= 3) c.latest_briefing = c.latest_briefing.slice(0, 600);
    const out = JSON.stringify({ ...c, bom });
    if (out.length <= 7000) return out;
  }
  return JSON.stringify({ ...c, bom: [] }).slice(0, 7000);
}

async function gemini(env, prompt) {
  const model = env.GEMINI_MODEL || "gemini-flash-lite-latest";
  const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
    body: JSON.stringify({ systemInstruction: { parts: [{ text: SYSTEM }] }, contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.4, maxOutputTokens: 1500 } }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j?.error?.message || `gemini ${r.status}`);
  const text = (j.candidates?.[0]?.content?.parts || []).map((p) => p.text || "").join("");
  if (!text.trim()) throw new Error("empty response");
  return { reply: text, model };
}

// The numbers a buyer asks for first, ahead of any scoring detail (keeps them inside the prompt budget).
function partFacts(part) {
  const s = part.snapshot || {};
  return {
    mpn: part.mpn, found: part.found, data_source: s.provider || part.source, data_date: s.day || s.data_day,
    lead_time_weeks: s.lead_days ? Math.round(s.lead_days / 7) : null, stock: s.total_avail ?? null,
    pbo_on_order_qty: s.on_order ?? null, pbo_next_arrival: s.on_order_eta ?? null,
    price_qty1_usd: s.price_1 ?? null, price_1k_usd: s.price_1k ?? null, lifecycle: s.lifecycle ?? null,
    country_of_origin: s.country_of_origin ?? null, us_hts: s.us_hts ?? null, tariff: s.tariff_note ?? null,
    segment: part.segment, supply_risk: part.supply_risk, price_trend: part.price_trend,
    outlook: s.outlook ? { action: s.outlook.action, why: s.outlook.why } : undefined,
    top_drivers: (part.signals || []).slice(0, 3).map((x) => x.evidence),
  };
}

async function handleChat(env, request, ctx) {
  if (!env.GROQ_API_KEY && !env.GEMINI_API_KEY) return json({ error: "No LLM key (GROQ_API_KEY) configured on the worker" }, 503);
  const body = await request.json().catch(() => ({}));
  const msgs = (body.messages || []).filter((m) => ["user", "assistant"].includes(m.role) && typeof m.content === "string")
    .slice(-8).map((m) => ({ role: m.role, content: m.content.slice(0, 4000) }));
  if (!msgs.length) return json({ error: "messages required" }, 400);
  const context = await siteJson(env, "context.json", ctx);
  let part = null;
  if (body.mpn) {
    const u = new URL(request.url);
    u.pathname = "/api/part";
    u.search = `?mpn=${encodeURIComponent(body.mpn)}`;
    part = await (await handlePart(env, u, ctx)).json();
  }
  const convo = msgs.slice(-6).map((m) => `${m.role.toUpperCase()}: ${m.content.slice(0, 1500)}`).join("\n");
  const prompt = `Customer market context (JSON):\n${compactContext(context, msgs[msgs.length - 1].content)}\n\n` +
    (part ? `Live part lookup:\n${JSON.stringify(partFacts(part))}\n\n` : "") +
    `Conversation:\n${convo}\n\nAnswer the last USER message.`;
  if (env.GROQ_API_KEY) {
    try { return json(await groq(env, prompt)); } catch (e) { if (!env.GEMINI_API_KEY) throw e; }
  }
  return json(await gemini(env, prompt));
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(request.url);
    if (url.pathname === "/api/health") {
      return json({ ok: true, llm: !!(env.GROQ_API_KEY || env.GEMINI_API_KEY), octopart: !!env.NEXAR_CLIENT_ID, mouser: !!env.MOUSER_API_KEY, gated: !!env.ACCESS_CODE });
    }
    if (env.ACCESS_CODE && request.headers.get("x-access-code") !== env.ACCESS_CODE) return json({ error: "invalid access code" }, 401);
    if (limited(request.headers.get("cf-connecting-ip") || "anon")) return json({ error: "slow down" }, 429);
    try {
      if (url.pathname === "/api/part" && request.method === "GET") return await handlePart(env, url, ctx);
      if (url.pathname === "/api/chat" && request.method === "POST") return await handleChat(env, request, ctx);
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: e.message }, 500);
    }
  },
};
