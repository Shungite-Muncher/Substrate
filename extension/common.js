// Shared settings + lookup logic for the popup.
const DEFAULTS = {
  site: "https://shungite-muncher.github.io/Substrate",
  api: "",
  code: "",
};

async function settings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  return { site: s.site.replace(/\/$/, ""), api: s.api.replace(/\/$/, ""), code: s.code };
}

const normalize = (m) => (m || "").replace(/\s+/g, "").toUpperCase();

async function lookup(mpn) {
  const s = await settings();
  mpn = normalize(mpn);
  // 1) free: the published BOM snapshot
  try {
    const parts = await (await fetch(`${s.site}/data/parts.json`, { cache: "no-cache" })).json();
    if (parts[mpn]) return { ...parts[mpn], snapshot: parts[mpn], via: "BOM snapshot" };
  } catch (e) { /* site unreachable; fall through */ }
  // 2) live API (Octopart first, cached at the edge)
  if (!s.api) throw new Error("Not in your BOM snapshot. Add the API URL in extension options for live lookups.");
  const r = await fetch(`${s.api}/api/part?mpn=${encodeURIComponent(mpn)}`, { headers: { "x-access-code": s.code } });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return { ...j, via: j.source || "live" };
}
