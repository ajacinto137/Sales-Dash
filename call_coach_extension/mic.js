document.getElementById("allow").addEventListener("click", async () => {
  const status = document.getElementById("status");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    status.textContent = "Microphone allowed. You can close this tab and press Start in the side panel.";
    chrome.runtime.sendMessage({ target: "sidepanel", type: "micAllowed" }).catch(() => {});
  } catch (err) {
    status.textContent = "Chrome blocked the microphone (" + err.name + "). Allow it from the icon in the address bar and try again.";
  }
});
