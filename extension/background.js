chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "substrate-lookup", title: "Look up “%s” in Substrate", contexts: ["selection"] });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "substrate-lookup") return;
  const mpn = (info.selectionText || "").replace(/\s+/g, "").toUpperCase().slice(0, 60);
  await chrome.storage.session.set({ pending: mpn });
  try {
    await chrome.action.openPopup();          // Chrome 127+
  } catch {
    const { site } = await chrome.storage.sync.get({ site: "https://shungite-muncher.github.io/Substrate" });
    chrome.tabs.create({ url: `${site.replace(/\/$/, "")}/?part=${encodeURIComponent(mpn)}` });
  }
});
