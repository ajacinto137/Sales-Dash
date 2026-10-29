// Lead Feeder (2026-10-28) -- renders the shared lead list from
// /lead-feeder/api/state and posts takes/outcomes. Every rule (who's
// eligible, ranking, claiming, the pre-dial HubSpot check) runs on the
// server; this file never decides eligibility. Keeps the original page's
// workflow: one reusable HubSpot tab opened on the click (so the browser
// allows it), "close the HubSpot tab = next lead", and No answer / Left VM
// / Skip loading the next lead automatically.
(() => {
  const root = document.getElementById("lead-feeder");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const OUTCOMES = JSON.parse($("lf-outcomes").textContent);
  const POLL_MS = 8000;
  const HS_WIN = "planetHubSpotLead";

  let state = null, busy = false, hsWin = null, hsWinFor = null, pollTimer = null, toastTimer = null;
  let planetwebFor = null;

  async function api(url, body) {
    const res = await fetch(url, body === undefined ? {credentials: "same-origin"} : {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { data = {ok: false, error: `Server error (${res.status}).`}; }
    if (!res.ok && data.ok !== false) data = {ok: false, error: `Server error (${res.status}).`};
    return data;
  }

  const fmtDate = s => {
    if (!s) return "Never";
    const d = new Date(s); if (isNaN(d)) return String(s);
    const thisYear = new Date().getFullYear();
    return d.toLocaleDateString(undefined, {month: "short", day: "numeric", year: d.getFullYear() !== thisYear ? "numeric" : undefined});
  };
  const pct = v => `${(v * 100).toFixed(1)}%`;

  function toast(text, bad) {
    const m = $("lf-msg");
    m.hidden = false; m.className = "td-lf-msg" + (bad ? " td-lf-msg-bad" : "");
    m.innerHTML = esc(text);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { if (state && state.leads.length) m.hidden = true; }, 6000);
  }

  // ---------- HubSpot window: one reusable tab, opened on the click ----------
  function preopen() {
    if (!state || !state.prefs.auto_hs) return null;
    try {
      const w = window.open("", HS_WIN);
      if (w) { try { w.document.title = "Loading lead…"; w.document.body.innerHTML = '<p style="font:16px system-ui;padding:24px;color:#555">Checking the lead, HubSpot opens in a second…</p>'; } catch (e) {} }
      return w;
    } catch (e) { return null; }
  }
  function sendTo(win, lead) {
    if (!win) return;
    hsWin = win; hsWinFor = lead.id;
    try { win.location.href = lead.hubspot_url; } catch (e) { try { window.open(lead.hubspot_url, HS_WIN); } catch (_) {} }
  }
  function closeWin(win) { try { if (win && !win.closed) win.close(); } catch (e) {} }

  // ---------- chips ----------
  function tierChip(l) {
    return `<span class="td-lf-tier td-lf-t${l.tier}" title="Priority score ${l.score}/100">${esc(l.tier_label)} · ${l.score}</span>`;
  }
  function kindChip(l) {
    if (l.kind === "sweep" && l.sweep) {
      const o = l.sweep.owner || {}, out = [`<span class="td-lf-chip td-lf-acc" title="Checked in PAS Planet Maps: Available Now, 0 orders">PAS verified ${esc(l.sweep.date)}</span>`];
      if (o.state === "none") out.push(`<span class="td-lf-chip td-lf-ok">Unowned</span>`);
      else if (o.state === "inactive") out.push(`<span class="td-lf-chip" title="Owner is an inactive rep">Owner: ${esc(o.name)} (inactive)</span>`);
      else { out.push(`<span class="td-lf-chip">Owner: ${esc(o.name || "assigned since the sweep")}</span>`); out.push(`<span class="td-lf-chip td-lf-bad">Owned by active rep, check with manager</span>`); }
      if (l.sweep.manual) out.push(`<span class="td-lf-chip td-lf-warn">Check manually</span>`);
      return out.join("");
    }
    if (l.kind === "quiet") return `<span class="td-lf-chip td-lf-warn" title="Owned SQL nobody has worked lately. Check with the owner if you're unsure.">Owned · ${l.quiet_days == null ? "never worked" : `quiet ${l.quiet_days} days`}</span>`;
    return "";
  }
  function signalChips(l) {
    const s = l.signals || {}, out = [];
    if (s.cameBack) out.push(`<span class="td-lf-chip td-lf-acc" title="Filled out a new prequal after their last call">Came back after last call</span>`);
    if (s.hot) out.push(`<span class="td-lf-chip td-lf-acc" title="Marked Hot in the PAS sweep">Hot</span>`);
    if (l.prio) out.push(`<span class="td-lf-chip td-lf-bad" title="${esc(l.prio.reason || "Put at the top of the list")}">Call first</span>`);
    if (l.prio_check_pas) out.push(`<span class="td-lf-chip td-lf-warn" title="HubSpot doesn't list this address as serviceable. Check PlanetWeb before you dial.">Check PAS</span>`);
    if (s.notInstalled) out.push(`<span class="td-lf-chip td-lf-acc" title="Someone with the same last name ordered at this address, but they aren't current in Vision. The order may never have been installed.">Family order not installed</span>`);
    if (s.stalled) out.push(`<span class="td-lf-chip td-lf-acc" title="Signed up, never ordered, called 2 times or less">Stalled signup</span>`);
    if (s.newly) out.push(`<span class="td-lf-chip td-lf-acc" title="Signed up before Planet could serve them. Their address is serviceable now.">Now serviceable</span>`);
    if (s.repeat) out.push(`<span class="td-lf-chip td-lf-acc" title="Number of times they filled out the prequal form">Prequaled ${s.repeat}×</span>`);
    if (s.lts != null && s.lts >= 9) out.push(`<span class="td-lf-chip td-lf-ok" title="Their own answer on the prequal form">Switch score ${s.lts}/10</span>`);
    if (s.goBack) out.push(`<span class="td-lf-chip">Go Back</span>`);
    if (s.price) out.push(`<span class="td-lf-chip td-lf-warn" title="Price objection, last touched 60+ days ago. Lead with the current offer.">Price objection, old</span>`);
    return out.join("");
  }

  // ---------- render ----------
  function renderBoard() {
    const list = state.leads;
    $("lf-open").textContent = state.pool.loaded ? String(state.open_count) : "–";
    const board = $("lf-board"), msg = $("lf-msg");
    if (!state.pool.loaded) {
      board.hidden = true; msg.hidden = false;
      msg.className = "td-lf-msg" + (state.pool.error ? " td-lf-msg-bad" : "");
      msg.innerHTML = state.pool.error ? `Couldn't load leads from HubSpot: ${esc(state.pool.error)}` : `<span class="td-lf-spin"></span> Pulling the shared list from HubSpot…`;
      $("lf-next").disabled = true;
      return;
    }
    board.hidden = false;
    $("lf-rank-hint").hidden = false;
    const off = !!state.current || busy;
    $("lf-next").disabled = off || !list.length;
    if (!list.length) { board.innerHTML = `<li class="td-lf-msg">Everyone on the list has been taken. Nice work. It refreshes from HubSpot every 3 minutes.</li>`; return; }
    board.innerHTML = list.map(l => `<li class="td-lf-lead">
        <div class="td-lf-who"><button type="button" data-take="${esc(l.id)}" ${off ? "disabled" : ""} title="Take this lead and open it in HubSpot">${esc(l.name)}</button><div class="td-lf-where">${esc(l.where)}</div></div>
        <button class="td-btn td-btn-primary td-lf-sm" type="button" data-take="${esc(l.id)}" ${off ? "disabled" : ""}>Take</button>
        <div class="td-lf-facts">
          ${tierChip(l)}${kindChip(l)}${signalChips(l)}
          ${l.replied ? `<span class="td-lf-chip td-lf-acc">Replied to email</span>` : ""}
          ${l.never_called ? `<span class="td-lf-chip td-lf-ok">Never called</span>` : `<span>${l.calls} call${l.calls === 1 ? "" : "s"} · last ${esc(fmtDate(l.last_called))}</span>`}
          ${l.no_texts ? `<span class="td-lf-chip td-lf-warn">No texts</span>` : ""}
          ${l.prequal_date ? `<span class="td-lf-chip">Prequal ${esc(fmtDate(l.prequal_date))}</span>` : ""}
        </div>
        <div class="td-lf-why">${esc(l.why.join(" · "))}</div>
        ${l.sweep && l.sweep.notes ? `<div class="td-lf-why"><b>Sweep notes:</b> ${esc(l.sweep.notes)}</div>` : ""}
      </li>`).join("") + (state.open_count > list.length ? `<li class="td-lf-lead"><div class="td-lf-where">+ ${state.open_count - list.length} more. Take some and they'll move up.</div></li>` : "");
    board.querySelectorAll("[data-take]").forEach(b => b.onclick = () => { if (!state.current && !busy) takeOne(b.dataset.take); });
  }

  function renderHints() {
    const p = state.pool;
    if (p.loaded) {
      $("lf-fresh").textContent = `Updated ${p.loaded_display} · refreshes every 3 min` + (p.weights_at ? ` · ranking learned ${p.weights_at}` : "") + (p.error ? ` · last refresh failed, showing the previous list` : "");
      const k = p.kinds || {}, cool = state.prefs.cool_days;
      const prio = k.priority || 0;
      let mix = (p.priority && p.priority.stored ? `Call first: ${state.leads.filter(l => l.prio).length} at the top. ` : "")
        + `Pulled everything in HubSpot (no cap): ${k.sql || 0} open SQLs`
        + (state.prefs.quiet_on ? `, ${k.quiet || 0} owned SQLs nobody's called in 30+ days` : "")
        + (state.prefs.sweep_on && p.sweep ? `, ${k.sweep || 0} PAS sweep (of ${p.sweep.stored} stored${Object.keys(p.sweep.out || {}).length ? `; screened out: ${Object.entries(p.sweep.out).sort((a, b) => b[1] - a[1]).map(([r, n]) => `${n} ${r.toLowerCase()}`).join(", ")}` : ""})` : "")
        + (prio ? `, ${prio} call-first` : "")
        + `. Skipping anyone called in the last ${cool === 1 ? "day" : cool + " days"}.`
        + (p.household && p.household.ready
          ? ` Install scheduled at the address: ${p.household.scheduled} left out; family already a current customer: ${p.household.current} left out; family order not installed: ${p.household.not_current} moved up.`
          : p.household ? " Family-order check is waiting for PlanetWeb orders and Vision to load." : "");
      if (p.capped) mix += " HubSpot's 10,000-result search limit was reached, so some older leads may be missing.";
      $("lf-mix").textContent = mix;
    } else {
      $("lf-fresh").textContent = p.configured ? "Not loaded yet" : "HubSpot isn't connected";
    }
    const h = state.intel && state.intel.hint, el = $("lf-time");
    if (h) {
      el.hidden = false;
      el.innerHTML = `<b>Pickup right now:</b> ${h.current != null ? `${pct(h.current)} (${h.above ? "above" : "below"} the ${pct(h.avg)} average)` : "not enough calls at this hour yet"} · <b>Best hours:</b> ${h.best.map(x => `${x.label} ${(x.r * 100).toFixed(0)}%`).join(", ")}`;
    } else el.hidden = true;
  }

  function bars(rows, width) {
    return `<div class="td-lf-bars">${rows.map(r => `<div class="td-lf-bar"><span>${esc(r.label)}</span><i style="width:${Math.min(100, width(r)).toFixed(1)}%"></i><span>${esc(r.value)}</span></div>`).join("")}</div>`;
  }
  let insKey = "";
  function renderInsights() {
    const v = state.intel, key = JSON.stringify(v);
    if (key === insKey) return; insKey = key;
    $("lf-ins-fresh").textContent = v.live && v.at ? `Measured ${new Date(v.at).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})} from all records` : (v.error ? "Using Oct 21 measurements (live check failed)" : "Using Oct 21 measurements until the first live check");
    const boxes = [];
    if (v.snapshot) boxes.push(`<div class="td-lf-ibox"><h3>Your database</h3><div class="td-lf-big td-cyan">${v.snapshot.total.toLocaleString()}</div>
      <p>contacts in HubSpot. ${v.snapshot.prospects.toLocaleString()} are still prospects (Lead, MQL, SQL, Opportunity), ${v.snapshot.sql.toLocaleString()} of them SQLs. The rest are customers, Do Not Contact or unusable.</p></div>`);
    if (v.conv.length) boxes.push(`<div class="td-lf-ibox"><h3>Speed matters most</h3><p>Of ${v.conv_n.toLocaleString()} SQLs who became customers, how many did it within…</p>
      ${bars(v.conv.map(c => ({label: `≤ ${c.days} days`, value: `${Math.round(c.share * 100)}%`, r: c.share})), r => r.r * 100)}
      <p>That's why fresh SQLs rank first. Some fast conversions may be people who ordered on their own.</p></div>`);
    if (v.calls) boxes.push(`<div class="td-lf-ibox"><h3>When calling stops paying off</h3><p>Share of SQLs that became customers, by how many times they've been called (customers vs. SQLs still open):</p>
      ${bars(v.calls.map(c => ({label: `${c.label} calls`, value: `${Math.round(c.rate * 100)}%`, r: c.rate})), r => r.r * 100)}
      <p>A correlation, not proof: people who buy early stop getting called. Still, lots of calls with no sale is a strong sign to move on.</p></div>`);
    if (v.connect.length) boxes.push(`<div class="td-lf-ibox"><h3>Pick-up rate per dial</h3><p>Outbound HubSpot calls marked Connected, by quarter (bar scaled to 20%):</p>
      ${bars(v.connect.map(c => ({label: c.label, value: pct(c.rate), r: c.rate})), r => r.r * 500)}
      <p>Roughly 1 in 11 dials connects.</p></div>`);
    if (v.hours) boxes.push(`<div class="td-lf-ibox"><h3>Best time to dial</h3><p>Team outbound calls marked Connected, last 30 days (${v.hours.n.toLocaleString()} calls, Eastern time, bar scaled to 20%):</p>
      ${bars(v.hours.hrs.map(c => ({label: c.label, value: pct(c.rate), r: c.rate})), r => r.r * 500)}
      ${bars(v.hours.wd.map(c => ({label: c.label, value: pct(c.rate), r: c.rate})), r => r.r * 500)}
      <p>Use it to save your freshest and highest-signal leads for the hours people actually pick up.</p></div>`);
    $("lf-ins-body").innerHTML = boxes.length ? `<div class="td-lf-insgrid">${boxes.join("")}</div>
      <p class="td-lf-hint"><b>How Priority works.</b> Score = (share of past SQL sales still ahead at this lead's SQL age) × (how well SQLs with this many calls have converted, relative to the best). P1 = SQL in the last 7 days, P2 = 8–30 days, P3 = 31–90 days, P4 = older but still in a call range that converts well, P5 = older and heavily called. Older leads jump up when they show a buying signal: a new prequal after their last call (up to P2), signing up before their address was serviceable or prequaling 3+ times (up to P3). Repeat prequals, a 9-10 switch score and 90+ days since the last dial also raise the score. Measured from HubSpot every hour.</p>`
      : `<p class="td-lf-hint">Couldn't measure yet.</p>`;
  }

  function renderTaken() {
    const rows = state.taken;
    $("lf-taken-count").textContent = String(rows.length);
    $("lf-mine-count").textContent = String(rows.filter(r => r.mine).length);
    $("lf-taken-card").hidden = !rows.length;
    $("lf-taken").innerHTML = rows.slice(0, 40).map(r => `<li class="td-lf-taken"><span>${esc(r.name)}</span><span>${esc(r.by)} · ${esc(r.outcome)} · ${esc(r.at)}</span></li>`).join("");
  }

  let mineKey = "";
  function renderMine() {
    const el = $("lf-mine"), c = state.current;
    if (!c) { el.hidden = true; mineKey = ""; return; }
    if (mineKey === c.id) return;   // don't redraw under the rep's cursor every poll
    mineKey = c.id;
    const chips = [tierChip(c)];
    if (c.no_texts) chips.push(`<span class="td-lf-chip td-lf-warn">No texts</span>`);
    if (c.never_called) chips.push(`<span class="td-lf-chip td-lf-ok">Never called</span>`); else if (c.calls <= 2) chips.push(`<span class="td-lf-chip td-lf-ok">Barely called</span>`);
    if (c.prequal_date) chips.push(`<span class="td-lf-chip">Prequal ${esc(fmtDate(c.prequal_date))}</span>`);
    if (c.replied) chips.push(`<span class="td-lf-chip td-lf-acc">Replied to email</span>`);
    chips.push(kindChip(c), signalChips(c));
    el.hidden = false;
    el.innerHTML = `
      <div class="td-lf-lamp"><i></i><span>You're on this one · hidden from the team</span></div>
      <p class="td-lf-name">${esc(c.name)}</p>
      <div class="td-lf-addr">${esc(c.address)}</div>
      <a class="td-lf-phone" id="lf-ph" href="${esc(c.tel)}" title="Call from your phone">${esc(c.phone)}</a>
      <div class="td-lf-row">
        <a class="td-btn td-btn-primary" href="${esc(c.tel)}">Call from my phone</a>
        <a class="td-btn" href="${esc(c.hubspot_url)}" target="${HS_WIN}" id="lf-open-hs">Open in HubSpot</a>
        <button class="td-btn" type="button" data-copy="${esc(c.phone)}">Copy number</button>
        <button class="td-btn" type="button" data-copy="${esc(c.address)}">Copy address</button>
      </div>
      <div class="td-lf-chips">${chips.join("")}</div>
      <div class="td-lf-why">Why this lead: ${esc(c.why.join(" · "))}</div>
      ${c.sweep && c.sweep.notes ? `<div class="td-lf-why"><b>Sweep notes:</b> ${esc(c.sweep.notes)}</div>` : ""}
      <div class="td-lf-meta">
        <div><span>Last called</span><b>${esc(fmtDate(c.last_called))}</b></div>
        <div><span>Calls so far</span><b>${esc(c.calls)}</b></div>
        <div><span>Lead status</span><b>${esc(c.lead_status || "—")}</b></div>
      </div>
      <div class="td-lf-planetweb" id="lf-pw">${state.prefs.auto_maps ? `<span class="td-lf-spin"></span> Looking up this address in PlanetWeb…` : `<button class="td-btn td-btn-ghost" type="button" id="lf-pw-btn">Look up in PlanetWeb</button>`}</div>
      <div class="td-lf-notes">${(c.notes || []).length ? c.notes.map(n => `<p><time>${esc(fmtDate(n.ts))}</time>${esc(String(n.text).slice(0, 220))}</p>`).join("") : `<p class="td-lf-hint">No call notes on file.</p>`}</div>
      <div class="td-lf-label">How did it go? Tapping one frees you for the next lead.</div>
      <div class="td-lf-keys">
        ${OUTCOMES.map((o, i) => `<button class="td-btn${i === 0 ? " td-btn-primary" : ""}" type="button" data-out="${esc(o)}">${o === "Skipped" ? "Skip, don't call" : esc(o)}</button>`).join("")}
        <button class="td-btn" type="button" data-out="Called">Done + take next</button>
      </div>`;
    el.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => {
      const label = b.textContent;
      navigator.clipboard.writeText(b.dataset.copy).then(() => { b.textContent = "Copied"; setTimeout(() => b.textContent = label, 1200); }, () => {});
    });
    el.querySelectorAll("[data-out]").forEach(b => b.onclick = () => finish(b.dataset.out));
    const pwBtn = $("lf-pw-btn"); if (pwBtn) pwBtn.onclick = () => loadPlanetweb(c);
    if (state.prefs.auto_maps) loadPlanetweb(c);
  }

  async function loadPlanetweb(c) {
    const el = $("lf-pw"); if (!el) return;
    planetwebFor = c.id;
    el.innerHTML = `<span class="td-lf-spin"></span> Looking up this address in PlanetWeb…`;
    const r = await api(root.dataset.planetwebUrl.replace("__ID__", encodeURIComponent(c.id)));
    if (planetwebFor !== c.id || !$("lf-pw")) return;
    if (!r.ok) { el.innerHTML = `<span class="td-lf-hint">PlanetWeb lookup failed: ${esc(r.error || "unknown error")}</span>`; return; }
    const parts = [`<div class="td-lf-label">PlanetWeb at this address</div>`];
    if (r.place) {
      const p = r.place;
      parts.push(`<div class="td-lf-chips"><span class="td-lf-chip ${/available now/i.test(p.availability) ? "td-lf-ok" : "td-lf-warn"}">${esc(p.availability)}</span>
        <span class="td-lf-chip ${p.order_status === "No Order" ? "td-lf-ok" : "td-lf-bad"}">${esc(p.order_status)}</span>
        ${p.became_available ? `<span class="td-lf-chip">Available since ${esc(p.became_available)}</span>` : ""}
        ${p.pas_url ? `<a class="td-lf-chip td-lf-acc" href="${esc(p.pas_url)}" target="planetPas" rel="noopener">Open in PAS ↗</a>` : ""}</div>
        <div class="td-lf-hint">${esc(p.address)}</div>`);
    } else {
      parts.push(`<div class="td-lf-hint">${r.snapshot_ready ? "No PlanetWeb passing matches this street and ZIP. Check PAS before you dial." : "PlanetWeb places are still loading. Try again in a minute."}</div>`);
    }
    if (r.orders.length) parts.push(`<div class="td-lf-hint"><b class="td-lf-warn-text">Existing order${r.orders.length > 1 ? "s" : ""} at this address:</b> ${r.orders.map(o => `<a href="${esc(o.profile_url)}" target="_blank" rel="noopener">${esc(o.name)} · ${esc(o.order_date)}</a>`).join(", ")}</div>`);
    el.innerHTML = parts.join("");
  }

  function render() {
    renderMine(); renderBoard(); renderHints(); renderTaken(); renderInsights();
  }

  async function poll() {
    const r = await api(root.dataset.stateUrl);
    if (!r.ok) { toast(r.error || "Couldn't reach the dashboard.", true); return; }
    state = r;
    render();
  }

  // ---------- take / finish ----------
  async function take(contactId, win) {
    if (busy) { closeWin(win); return; }
    busy = true; render();
    const r = await api(root.dataset.takeUrl, contactId ? {contact_id: contactId} : {});
    busy = false;
    if (r.messages && r.messages.length) toast(r.messages.join(" "));
    if (!r.ok || !r.lead) {
      closeWin(win);
      if (r.error) toast(r.error, true);
      await poll();
      return;
    }
    sendTo(win, r.lead);
    await poll();
  }
  const takeOne = id => take(id, preopen());
  const takeNext = win => take(null, win);

  async function finish(outcome) {
    if (!state.current || busy) return;
    const c = state.current;
    const next = ["No answer", "Left VM", "Skipped", "Called"].includes(outcome);
    const win = next ? preopen() : null;   // open on the click so the browser allows it
    busy = true;
    const r = await api(root.dataset.outcomeUrl, {contact_id: c.id, outcome});
    busy = false;
    if (!r.ok) { closeWin(win); toast(r.error || "Couldn't save that.", true); return; }
    if (hsWinFor === c.id) { hsWin = null; hsWinFor = null; }
    state.current = null;
    if (next) takeNext(win); else poll();
  }

  // ---------- close the HubSpot tab = next lead ----------
  // Closing the tab is NOT treated as a completed call: it's recorded as
  // "Tab closed". Whether a call happened comes from HubSpot's own call
  // records (Admin > Lead Feeder).
  setInterval(async () => {
    if (!hsWin || !hsWin.closed || busy) return;
    const forId = hsWinFor; hsWin = null; hsWinFor = null;
    if (!state || !state.current || state.current.id !== forId) return;
    busy = true;
    const r = await api(root.dataset.outcomeUrl, {contact_id: forId, outcome: "Tab closed"});
    busy = false;
    state.current = null;
    if (!r.ok || !state.prefs.auto_hs) { poll(); return; }
    const w = preopen();
    if (w) { takeNext(w); return; }
    const m = $("lf-msg"); m.hidden = false; m.className = "td-lf-msg";
    m.innerHTML = `Your browser blocked the next tab. Click the blocked-popup icon in the address bar and choose <b>Always allow</b>, then <button class="td-btn td-btn-primary td-lf-sm" type="button" id="lf-resume">Open next lead</button>`;
    $("lf-resume").onclick = () => { m.hidden = true; takeNext(preopen()); };
    poll();
  }, 700);

  // ---------- filters (saved per rep on the server) ----------
  root.querySelectorAll("[data-pref]").forEach(input => {
    input.addEventListener("change", async () => {
      const key = input.dataset.pref;
      const value = input.type === "checkbox" ? input.checked : (key === "cool_days" ? Number(input.value) : input.value);
      const body = {[key]: value};
      // an area lives in one state; picking another state clears the area (like the page)
      if (key === "state") {
        const area = root.querySelector('[data-pref="area"]');
        if (area && area.value !== "all") { area.value = "all"; body.area = "all"; }
      }
      const r = await api(root.dataset.prefsUrl, body);
      if (!r.ok) { toast(r.error || "Couldn't save that setting.", true); return; }
      const st = root.querySelector('[data-pref="state"]'); if (st) st.value = r.prefs.state;
      root.querySelectorAll("select[data-pref]").forEach(s => s.classList.toggle("td-lf-on", s.value !== "all" && s.dataset.pref !== "cool_days"));
      poll();
    });
  });
  root.querySelectorAll("select[data-pref]").forEach(s => s.classList.toggle("td-lf-on", s.value !== "all" && s.dataset.pref !== "cool_days"));

  $("lf-next").onclick = () => { if (!state || state.current || busy) return; takeNext(preopen()); };

  poll();
  pollTimer = setInterval(() => { if (document.visibilityState === "visible" && !busy) poll(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(); });
})();
