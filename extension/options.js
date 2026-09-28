const ids = ["site", "api", "code"];
chrome.storage.sync.get(DEFAULTS).then((s) => ids.forEach((k) => (document.getElementById(k).value = s[k])));
document.getElementById("save").addEventListener("click", async () => {
  const v = Object.fromEntries(ids.map((k) => [k, document.getElementById(k).value.trim()]));
  await chrome.storage.sync.set(v);
  document.getElementById("ok").textContent = "Saved";
  setTimeout(() => (document.getElementById("ok").textContent = ""), 1500);
});
