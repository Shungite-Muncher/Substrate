/**
 * Substrate API worker (Cloudflare Workers free tier).
 *
 *   GET  /api/health
 *   GET  /api/part?mpn=...   BOM snapshot -> edge cache -> Octopart (Nexar) -> Mouser
 *   POST /api/chat           { messages: [{role, content}], mpn? }
 *
 * Secrets (wrangler secret put ...): GEMINI_API_KEY, NEXAR_CLIENT_ID, NEXAR_CLIENT_SECRET,
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
- Keep answers under 350 words unless asked for more. Markdown.`;

const NEXAR_QUERY = `query SubstratePart($q: String!) {
  supSearchMpn(q: $q, limit: 1) { results { part {
    mpn manufacturer { name } shortDescription category { name path } octopartUrl totalAvail
    estimatedFactoryLeadDays medianPrice1000 { price currency }
    sellers(authorizedOnly: true) { company { name } offers { inventoryLevel factoryLeadDays moq prices { quantity price currency } } }
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
  const r = await fetch("https://api.nexar.com/graphql/", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${nexarToken.value}` },
    body: JSON.stringify({ query: NEXAR_QUERY, variables: { q: mpn } }),
  });
  if (!r.ok) throw new Error(`nexar ${r.status}`);
  const p = (await r.json())?.data?.supSearchMpn?.results?.[0]?.part;
  if (!p) return null;
  const sellers = [];
  for (const s of p.sellers || []) for (const o of s.offers || []) {
    sellers.push({ seller: s.company.name, stock: o.inventoryLevel, lead_days: o.factoryLeadDays, moq: o.moq,
      price_1k: priceAt((o.prices || []).filter((x) => !x.currency || x.currency === "USD").map((x) => [x.quantity, x.price])) });
  }
  return { mpn: p.mpn || mpn, provider: "octopart", manufacturer: p.manufacturer?.name, description: p.shortDescription,
    category: p.category?.path || p.category?.name, total_avail: p.totalAvail, lead_days: p.estimatedFactoryLeadDays,
    price_1k: p.medianPrice1000?.price, url: p.octopartUrl, sellers: sellers.slice(0, 15), day: new Date().toISOString().slice(0, 10) };
}

async function mouserLookup(env, mpn) {
  const r = await fetch(`https://api.mouser.com/api/v1/search/partnumber?apiKey=${encodeURIComponent(env.MOUSER_API_KEY)}`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ SearchByPartRequest: { mouserPartNumber: mpn, partSearchOptions: "Exact" } }),
  });
  if (!r.ok) throw new Error(`mouser ${r.status}`);
  const parts = (await r.json())?.SearchResults?.Parts || [];
  const p = parts.find((x) => (x.ManufacturerPartNumber || "").toUpperCase() === mpn) || parts[0];
  if (!p) return null;
  const money = (s) => { const m = String(s || "").match(/[\d.,]+/); return m ? parseFloat(m[0].replace(/,/g, "")) : null; };
  const stock = parseInt(String(p.AvailabilityInStock || "").replace(/,/g, ""), 10);
  const lm = String(p.LeadTime || "").match(/(\d+)\s*(week|day)?/i);
  const lead = lm ? (+lm[1]) * (/week/i.test(lm[2] || "") ? 7 : 1) : null;
  const price = priceAt((p.PriceBreaks || []).map((b) => [+b.Quantity, money(b.Price)]));
  return { mpn: p.ManufacturerPartNumber || mpn, provider: "mouser", manufacturer: p.Manufacturer, description: p.Description,
    category: p.Category, total_avail: isNaN(stock) ? null : stock, lead_days: lead, price_1k: price, lifecycle: p.LifecycleStatus,
    url: p.ProductDetailUrl, sellers: [{ seller: "Mouser", stock: isNaN(stock) ? null : stock, lead_days: lead, price_1k: price }],
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
  const key = new Request(`https://substrate-cache.internal/part/${encodeURIComponent(mpn)}`);
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
      norm: tanh((snap.lead_days - BASELINE_LEAD_DAYS) / 60), evidence: `Factory lead time ${Math.round(snap.lead_days / 7)} weeks vs a ~12-week norm (${snap.provider})`, url: snap.url });
    if (snap.total_avail != null) sigs.push({ name: "Channel inventory cover", source: src, component: "supply", weight: 0.20,
      norm: tanh((4 - Math.log10(snap.total_avail + 1)) / 1.5), evidence: `Channel stock ${snap.total_avail.toLocaleString()} units across authorized sellers`, url: snap.url });
    const n = new Set((snap.sellers || []).filter((s) => (s.stock || 0) > 0).map((s) => s.seller)).size;
    if ((snap.sellers || []).length) sigs.push({ name: "Sources with stock", source: src, component: "supply", weight: 0.05,
      norm: tanh((3 - n) / 2), evidence: `${n} authorized seller(s) currently holding stock`, url: snap.url });
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
    ? (s >= 75 ? "Severe" : s >= 62 ? "Elevated" : s >= 54 ? "Rising" : s > 46 ? "Neutral" : s > 38 ? "Easing" : "Low")
    : (s >= 72 ? "Rising fast" : s >= 60 ? "Rising" : s >= 53 ? "Firming" : s > 47 ? "Flat" : s > 40 ? "Softening" : "Falling");
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

async function handleChat(env, request, ctx) {
  if (!env.GEMINI_API_KEY) return json({ error: "GEMINI_API_KEY not configured on the worker" }, 503);
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
  const convo = msgs.map((m) => `${m.role.toUpperCase()}: ${m.content}`).join("\n");
  const prompt = `Customer market context (JSON):\n${JSON.stringify(context).slice(0, 24000)}\n\n` +
    (part ? `Live part lookup:\n${JSON.stringify(part).slice(0, 4000)}\n\n` : "") +
    `Conversation:\n${convo}\n\nAnswer the last USER message.`;
  return json(await gemini(env, prompt));
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(request.url);
    if (url.pathname === "/api/health") {
      return json({ ok: true, llm: !!env.GEMINI_API_KEY, octopart: !!env.NEXAR_CLIENT_ID, mouser: !!env.MOUSER_API_KEY, gated: !!env.ACCESS_CODE });
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
