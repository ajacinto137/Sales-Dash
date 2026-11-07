// Live Call Coach -- capture + live transcription (offscreen document).
// Channel 0 = rep microphone, channel 1 = HubSpot tab (the customer).
// Audio goes to Deepgram as 16 kHz stereo PCM; transcript events go to
// the side panel. Nothing is recorded to disk and no audio is kept.

const SPEAKERS = ["rep", "customer"];
const DEEPGRAM_PARAMS = new URLSearchParams({
  model: "nova-3",
  encoding: "linear16",
  sample_rate: "16000",
  channels: "2",
  multichannel: "true",
  interim_results: "true",
  punctuate: "true",
  smart_format: "true",
  endpointing: "300",
  utterance_end_ms: "1000",
});

let state = null;

function toPanel(message) {
  chrome.runtime.sendMessage({ target: "sidepanel", ...message }).catch(() => {});
}

async function getToken(serverUrl) {
  const resp = await fetch(serverUrl + "/api/call-coach/stt-token", { method: "POST", credentials: "include" });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok || !body.ok) throw new Error(body.error || "Couldn't get a transcription token (HTTP " + resp.status + ").");
  return body.token;
}

function openSocket(serverUrl) {
  return getToken(serverUrl).then((token) => new Promise((resolve, reject) => {
    const ws = new WebSocket("wss://api.deepgram.com/v1/listen?" + DEEPGRAM_PARAMS, ["bearer", token]);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => resolve(ws);
    ws.onerror = () => reject(new Error("Couldn't connect to Deepgram."));
    ws.onmessage = (event) => handleDeepgram(event.data);
    ws.onclose = (event) => {
      if (!state || state.stopping) return;
      toPanel({ type: "status", level: "warn", text: "Transcription dropped (" + event.code + "), reconnecting..." });
      reconnect();
    };
  }));
}

async function reconnect() {
  if (!state || state.reconnecting) return;
  state.reconnecting = true;
  for (let attempt = 1; attempt <= 5 && state && !state.stopping; attempt++) {
    try {
      state.ws = await openSocket(state.serverUrl);
      toPanel({ type: "status", level: "ok", text: "Listening" });
      break;
    } catch (err) {
      await new Promise((r) => setTimeout(r, attempt * 1000));
    }
  }
  if (state) state.reconnecting = false;
}

function handleDeepgram(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch (_) { return; }
  if (msg.type === "Results") {
    const channel = (msg.channel_index || [0])[0];
    const text = ((msg.channel && msg.channel.alternatives && msg.channel.alternatives[0]) || {}).transcript || "";
    toPanel({
      type: "transcript",
      speaker: SPEAKERS[channel] || "customer",
      text,
      isFinal: !!msg.is_final,
      speechFinal: !!msg.speech_final,
    });
  } else if (msg.type === "UtteranceEnd") {
    const channel = (msg.channel || [0])[0];
    toPanel({ type: "utteranceEnd", speaker: SPEAKERS[channel] || "customer" });
  }
}

async function start(streamId, serverUrl) {
  if (state) await stop();
  state = { serverUrl, stopping: false, reconnecting: false };

  const tabStream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    video: false,
  });
  let micStream;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    tabStream.getTracks().forEach((t) => t.stop());
    state = null;
    throw new Error("MIC_BLOCKED");
  }

  const ctx = new AudioContext({ sampleRate: 16000 });
  await ctx.audioWorklet.addModule("pcm-worklet.js");
  const tabSource = ctx.createMediaStreamSource(tabStream);
  const micSource = ctx.createMediaStreamSource(micStream);
  // Tab capture mutes the tab -- play the customer back so the rep still hears them.
  tabSource.connect(ctx.destination);

  const merger = ctx.createChannelMerger(2);
  micSource.connect(merger, 0, 0);
  tabSource.connect(merger, 0, 1);
  const worklet = new AudioWorkletNode(ctx, "pcm-worklet", { channelCount: 2, channelCountMode: "explicit" });
  merger.connect(worklet);
  worklet.port.onmessage = (event) => {
    if (state && state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(event.data);
  };

  Object.assign(state, { tabStream, micStream, ctx, worklet });
  state.ws = await openSocket(serverUrl);
  toPanel({ type: "status", level: "ok", text: "Listening" });
}

async function stop() {
  if (!state) return;
  const s = state;
  s.stopping = true;
  state = null;
  try {
    if (s.ws && s.ws.readyState === WebSocket.OPEN) {
      s.ws.send(JSON.stringify({ type: "CloseStream" }));
      s.ws.close();
    }
  } catch (_) { /* already closed */ }
  [s.tabStream, s.micStream].forEach((stream) => stream && stream.getTracks().forEach((t) => t.stop()));
  if (s.ctx) await s.ctx.close().catch(() => {});
  toPanel({ type: "status", level: "idle", text: "Stopped" });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "offscreen") return false;
  if (msg.type === "start") {
    start(msg.streamId, msg.serverUrl)
      .then(() => sendResponse({ ok: true }))
      .catch((err) => { stop(); sendResponse({ ok: false, error: String(err.message || err) }); });
    return true;
  }
  if (msg.type === "stop") {
    stop().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});
