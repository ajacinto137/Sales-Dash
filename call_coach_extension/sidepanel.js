// Live Call Coach -- side panel. Shows the transcript, and after every
// finished customer line asks Sales-Dash for (1) instant playbook cards
// and (2) a Claude "say this next" card. Cards are only shown here.

const $ = (id) => document.getElementById(id);
const lines = [];                       // [{speaker, text}]
const pending = { rep: "", customer: "" };
let suggestInFlight = false;
let suggestQueued = false;
let running = false;

function setStatus(text, level) {
  const el = $("status");
  el.textContent = text;
  el.className = "status " + (level || "idle");
}

function showSetup(html) {
  const el = $("setup");
  if (!html) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = html;
}

async function api(path, body) {
  const resp = await fetch(SERVER_URL + path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "include",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401) throw Object.assign(new Error("signed_out"), { signedOut: true });
  if (!resp.ok || !data.ok) throw new Error(data.error || "HTTP " + resp.status);
  return data;
}

async function checkSession() {
  try {
    const s = await api("/api/call-coach/session");
    const missing = [];
    if (!s.deepgram_ready) missing.push("Deepgram (live transcription)");
    if (!s.anthropic_ready) missing.push("Anthropic (smart cards)");
    showSetup(missing.length ? "Missing API key: " + missing.join(", ") + ". An Admin can add it on " +
      '<a href="' + SERVER_URL + '/tools/call-coach" target="_blank">Tools &gt; Live Call Coach</a>.' : "");
    if (s.consent_line) {
      $("consent").hidden = false;
      $("consent").textContent = 'Say first: "' + s.consent_line + '"';
    }
    return s;
  } catch (err) {
    if (err.signedOut) {
      showSetup('Sign in to <a href="' + SERVER_URL + '/login" target="_blank">Sales-Dash</a> in this Chrome profile, then reopen this panel.');
    } else {
      showSetup("Can't reach Sales-Dash at " + SERVER_URL + " (" + err.message + ").");
    }
    return null;
  }
}

// ------------------------------------------------------------ cards

function renderCard(card) {
  const el = $("card");
  el.className = "card " + card.kind;
  el.innerHTML = "";
  const source = document.createElement("span");
  source.className = "card-source";
  source.textContent = card.source === "ai" ? "AI" : "Playbook";
  const label = document.createElement("div");
  label.className = "card-label";
  label.textContent = card.label;
  el.append(source, label);
  if (card.heardLine) {
    const heard = document.createElement("div");
    heard.className = "card-heard";
    heard.textContent = '"' + card.heardLine + '"';
    el.append(heard);
  }
  const say = document.createElement("div");
  say.className = "card-say";
  say.textContent = card.say;
  el.append(say);
  if (card.why) {
    const why = document.createElement("div");
    why.className = "card-why";
    why.textContent = card.why;
    el.append(why);
  }
}

let currentCard = null;
function showCard(card) {
  if (currentCard) {
    const li = document.createElement("li");
    const label = document.createElement("div");
    label.className = "h-label";
    label.textContent = currentCard.label;
    const say = document.createElement("div");
    say.textContent = currentCard.say;
    li.append(label, say);
    $("history").prepend(li);
  }
  currentCard = card;
  renderCard(card);
}

// ------------------------------------------------------- transcript

function addLine(speaker, text) {
  text = text.trim();
  if (!text) return;
  lines.push({ speaker, text });
  const p = document.createElement("p");
  p.className = speaker;
  const who = document.createElement("b");
  who.textContent = speaker === "rep" ? "You: " : "Customer: ";
  p.append(who, document.createTextNode(text));
  const box = $("transcript");
  box.append(p);
  box.scrollTop = box.scrollHeight;
  if (speaker === "customer") onCustomerLine(text);
}

function flush(speaker) {
  const text = pending[speaker];
  pending[speaker] = "";
  addLine(speaker, text);
}

async function onCustomerLine(text) {
  try {
    const { cards } = await api("/api/call-coach/match", { text });
    if (cards && cards.length) showCard({ ...cards[0], heardLine: text });
  } catch (err) { if (err.signedOut) checkSession(); }
  requestSuggestion();
}

async function requestSuggestion() {
  if (suggestInFlight) { suggestQueued = true; return; }
  suggestInFlight = true;
  const heardLine = lines.length ? lines[lines.length - 1].text : "";
  try {
    const { card } = await api("/api/call-coach/suggest", { lines, context: $("context").value });
    if (card) showCard({ ...card, heardLine });
  } catch (err) {
    if (err.signedOut) checkSession();
    else setStatus("Listening (AI cards: " + err.message + ")", "warn");
  } finally {
    suggestInFlight = false;
    if (suggestQueued) { suggestQueued = false; requestSuggestion(); }
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== "sidepanel") return;
  if (msg.type === "status") {
    setStatus(msg.text, msg.level);
  } else if (msg.type === "transcript") {
    if (msg.isFinal && msg.text) pending[msg.speaker] = (pending[msg.speaker] + " " + msg.text).trim();
    if (msg.speechFinal) flush(msg.speaker);
    const live = msg.isFinal ? pending[msg.speaker] : (pending[msg.speaker] + " " + msg.text).trim();
    $("interim").textContent = live ? (msg.speaker === "rep" ? "You: " : "Customer: ") + live : "";
  } else if (msg.type === "utteranceEnd") {
    flush(msg.speaker);
    $("interim").textContent = "";
  } else if (msg.type === "micAllowed") {
    showSetup("");
  }
});

// --------------------------------------------------------- controls

$("start").addEventListener("click", async () => {
  if (running) return;
  $("start").disabled = true;
  setStatus("Starting...", "warn");
  const session = await checkSession();
  if (!session) { $("start").disabled = false; setStatus("Not started", "idle"); return; }
  const reply = await chrome.runtime.sendMessage({ target: "background", type: "start" });
  $("start").disabled = false;
  if (!reply || !reply.ok) {
    const error = (reply && reply.error) || "Couldn't start.";
    if (error.includes("MIC_BLOCKED")) {
      showSetup('The coach needs your microphone once. <button id="mic-setup" class="link">Allow microphone</button>, then press Start again.');
      $("mic-setup").addEventListener("click", () => chrome.runtime.sendMessage({ target: "background", type: "openMicSetup" }));
    } else {
      showSetup(error);
    }
    setStatus("Not started", "error");
    return;
  }
  running = true;
  $("start").hidden = true;
  $("stop").hidden = false;
});

$("stop").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ target: "background", type: "stop" });
  flush("rep");
  flush("customer");
  running = false;
  $("start").hidden = false;
  $("stop").hidden = true;
  setStatus("Stopped", "idle");
});

$("copy").addEventListener("click", () => {
  const text = lines.map((l) => (l.speaker === "rep" ? "You: " : "Customer: ") + l.text).join("\n");
  navigator.clipboard.writeText(text);
});

checkSession();
