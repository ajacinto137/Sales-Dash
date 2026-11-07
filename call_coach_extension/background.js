// Live Call Coach -- service worker.
importScripts("config.js");
// Clicking the toolbar icon on the HubSpot tab opens the side panel and
// remembers that tab (the click grants activeTab, which tab capture needs).
// The side panel asks us to start/stop; capture and transcription run in
// an offscreen document because service workers can't hold media streams.

let callTabId = null;

chrome.action.onClicked.addListener(async (tab) => {
  callTabId = tab.id;
  await chrome.storage.session.set({ callTabId: tab.id, callTabTitle: tab.title || "" });
  await chrome.sidePanel.open({ tabId: tab.id });
});

async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Record the HubSpot call tab and microphone for live transcription.",
  });
}

async function closeOffscreen() {
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) await chrome.offscreen.closeDocument();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "background") return false;
  (async () => {
    try {
      if (msg.type === "start") {
        const stored = await chrome.storage.session.get("callTabId");
        const tabId = callTabId ?? stored.callTabId;
        if (tabId == null) throw new Error("Click the Call Coach icon on the HubSpot tab first.");
        const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
        await ensureOffscreen();
        const reply = await chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId, serverUrl: SERVER_URL });
        if (!reply || !reply.ok) throw new Error((reply && reply.error) || "Couldn't start recording.");
        sendResponse({ ok: true });
      } else if (msg.type === "stop") {
        await chrome.runtime.sendMessage({ target: "offscreen", type: "stop" }).catch(() => {});
        await closeOffscreen();
        sendResponse({ ok: true });
      } else if (msg.type === "openMicSetup") {
        await chrome.tabs.create({ url: chrome.runtime.getURL("mic.html") });
        sendResponse({ ok: true });
      }
    } catch (err) {
      sendResponse({ ok: false, error: String(err.message || err) });
    }
  })();
  return true;
});

