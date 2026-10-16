// Admin Portal -- HubSpot Integration (templates/admin/hubspot.html).
// "Sync HubSpot Calls" starts a background sync (POST /admin/hubspot/sync)
// and polls /admin/hubspot/status every few seconds while it runs;
// "Test Connection" shows a per-endpoint check (HTTP status + any missing
// scope HubSpot reports); mapping selects auto-save like admin_sales_reps.js.
// Server-side @auth.admin_required is the real access control.
document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("hubspot-integration");
    if (!root) return;

    const syncBtn = root.querySelector("[data-hs-sync]");
    const testBtn = root.querySelector("[data-hs-test]");
    const actionStatus = root.querySelector("[data-hs-action-status]");
    const errorEl = root.querySelector("[data-hs-error]");
    const checksEl = root.querySelector("[data-hs-checks]");
    const runEl = root.querySelector("[data-hs-run]");
    let pollTimer = null;
    // A sync is "done" once the latest run is newer than this id and no
    // longer running -- works even if it failed before the first poll.
    let baselineRunId = parseInt(root.getAttribute("data-latest-run-id") || "0", 10);
    let waitingForRun = false;
    let emptyPolls = 0;

    function showAction(state, message) {
        actionStatus.textContent = message;
        actionStatus.setAttribute("data-save-state", state);
        actionStatus.style.display = message ? "inline" : "none";
    }

    function postJson(url, body) {
        return fetch(url, { method: "POST", body: body || new URLSearchParams() })
            .then(function (res) {
                return res.json().catch(function () { return {}; }).then(function (data) {
                    return { ok: res.ok && data.ok !== false, status: res.status, data: data };
                });
            });
    }

    function fmtTime(iso) {
        if (!iso) return "—";
        return new Date(iso).toLocaleString("en-US", {
            timeZone: "America/New_York", month: "numeric", day: "numeric", year: "numeric",
            hour: "numeric", minute: "2-digit", timeZoneName: "short",
        }).replace(",", "");
    }

    function set(key, value) {
        root.querySelectorAll('[data-hs="' + key + '"]').forEach(function (el) { el.textContent = value; });
    }

    function renderRun(run) {
        if (!run) return;
        const parts = [
            ["Status", run.status.charAt(0).toUpperCase() + run.status.slice(1), "run_status"],
            ["Processed", run.records_processed.toLocaleString(), "records_processed"],
            ["New", run.new_calls.toLocaleString(), "new_calls"],
            ["Updated", run.updated_calls.toLocaleString(), "updated_calls"],
            ["Started", fmtTime(run.started_at), "run_started"],
        ];
        runEl.innerHTML = "";
        parts.forEach(function (p) {
            const wrap = document.createElement("span");
            const label = document.createElement("span");
            label.className = "td-hubspot-run-label";
            label.textContent = p[0];
            const value = document.createElement("span");
            value.setAttribute("data-hs", p[2]);
            value.textContent = p[1];
            wrap.appendChild(label);
            wrap.appendChild(document.createTextNode(" "));
            wrap.appendChild(value);
            runEl.appendChild(wrap);
        });
    }

    function applyStatus(s) {
        set("last_success_at", fmtTime(s.last_success_at));
        set("owners_total", s.owners_total.toLocaleString());
        set("owners_mapped", s.owners_mapped.toLocaleString());
        set("owners_unmatched", s.owners_unmatched.toLocaleString());
        set("outbound_calls_total", s.outbound_calls_total.toLocaleString());
        set("calls_total", s.calls_total.toLocaleString());
        const run = s.latest_run;
        if (run) {
            set("sync_status", run.status.charAt(0).toUpperCase() + run.status.slice(1) + " · " + (run.kind || "") + " (" + run.trigger + ")");
            renderRun(run);
        }
        const conn = root.querySelector('[data-hs="connection"]');
        if (s.configured && s.connection_ok !== null) {
            conn.innerHTML = s.connection_ok ? '<span class="td-green">Connected</span>' : '<span class="td-orange">Error</span>';
        }
        const running = run && run.status === "running";
        errorEl.textContent = s.last_error || "";
        errorEl.style.display = s.last_error && !running ? "block" : "none";
        return running;
    }

    function poll() {
        fetch("/admin/hubspot/status", { headers: { Accept: "application/json" } })
            .then(function (res) { return res.json(); })
            .then(function (s) {
                const running = applyStatus(s);
                const run = s.latest_run;
                if (running) {
                    syncBtn.disabled = true;
                    showAction("saving", "Syncing…");
                    pollTimer = window.setTimeout(poll, 3000);
                } else if (run && run.id > baselineRunId) {
                    // Finished -- reload so the mapping table reflects
                    // any newly discovered owners/matches.
                    window.location.reload();
                } else if (waitingForRun && emptyPolls++ < 15) {
                    // Sync thread may not have recorded its run row yet.
                    pollTimer = window.setTimeout(poll, 2000);
                } else {
                    syncBtn.disabled = false;
                    showAction("", "");
                }
            })
            .catch(function () { pollTimer = window.setTimeout(poll, 5000); });
    }

    syncBtn.addEventListener("click", function () {
        syncBtn.disabled = true;
        showAction("saving", "Starting sync…");
        postJson("/admin/hubspot/sync").then(function (result) {
            if (!result.ok) {
                syncBtn.disabled = false;
                showAction("error", result.data.error || "Could not start the sync.");
                return;
            }
            waitingForRun = true;
            emptyPolls = 0;
            window.clearTimeout(pollTimer);
            poll();
        }).catch(function () {
            syncBtn.disabled = false;
            showAction("error", "Network error -- please try again.");
        });
    });

    testBtn.addEventListener("click", function () {
        testBtn.disabled = true;
        showAction("saving", "Testing…");
        checksEl.style.display = "none";
        postJson("/admin/hubspot/test").then(function (result) {
            testBtn.disabled = false;
            const data = result.data;
            showAction(data.ok ? "ok" : "error", data.ok ? "Connection OK" : "Connection problem");
            checksEl.innerHTML = "";
            if (data.error) {
                checksEl.textContent = data.error;
            }
            (data.checks || []).forEach(function (c) {
                const row = document.createElement("div");
                row.className = "td-hubspot-check " + (c.ok ? "td-hubspot-check--ok" : "td-hubspot-check--fail");
                const head = document.createElement("strong");
                head.textContent = (c.ok ? "✓ " : "✕ ") + c.endpoint + (c.status ? " — HTTP " + c.status : "");
                row.appendChild(head);
                if (!c.ok && c.required_scopes && c.required_scopes.length) {
                    const scopes = document.createElement("div");
                    scopes.textContent = "Missing scope(s): " + c.required_scopes.join(", ");
                    row.appendChild(scopes);
                }
                if (c.detail) {
                    const detail = document.createElement("div");
                    detail.className = "td-admin-muted";
                    detail.textContent = c.detail;
                    row.appendChild(detail);
                }
                checksEl.appendChild(row);
            });
            checksEl.style.display = "flex";
        }).catch(function () {
            testBtn.disabled = false;
            showAction("error", "Network error -- please try again.");
        });
    });

    const refreshBtn = document.querySelector("[data-hs-refresh-mappings]");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", function () {
            refreshBtn.disabled = true;
            postJson("/admin/hubspot/mappings/refresh").then(function (result) {
                if (result.ok) {
                    window.location.reload();
                    return;
                }
                refreshBtn.disabled = false;
                showAction("error", result.data.error || "Could not refresh mappings.");
            });
        });
    }

    document.querySelectorAll("[data-mapping-row]").forEach(function (row) {
        const ownerId = row.getAttribute("data-owner-id");
        const select = row.querySelector("[data-mapping-select]");
        const status = row.querySelector("[data-save-status]");
        let lastSaved = select.value;

        select.addEventListener("change", function () {
            const body = new URLSearchParams();
            if (select.value === "auto" || select.value === "unassign") {
                body.append("action", select.value);
            } else {
                body.append("action", "assign");
                body.append("sales_rep_id", select.value);
            }
            select.disabled = true;
            status.textContent = "Saving…";
            status.setAttribute("data-save-state", "saving");
            status.style.display = "inline";
            postJson("/admin/hubspot/mappings/" + encodeURIComponent(ownerId), body).then(function (result) {
                select.disabled = false;
                if (!result.ok) {
                    select.value = lastSaved;
                    status.textContent = result.data.error || "Could not save.";
                    status.setAttribute("data-save-state", "error");
                    return;
                }
                lastSaved = select.value;
                // Reload so Status/method (and any auto re-match) show
                // what the server actually stored.
                window.location.reload();
            }).catch(function () {
                select.disabled = false;
                select.value = lastSaved;
                status.textContent = "Network error -- please try again.";
                status.setAttribute("data-save-state", "error");
            });
        });
    });

    // ---------------- Service key ----------------

    const keyForm = root.querySelector("[data-hs-key-form]");
    const keyStatus = root.querySelector("[data-hs-key-status]");
    const keyRemove = root.querySelector("[data-hs-key-remove]");

    function showKeyStatus(state, message) {
        keyStatus.textContent = message;
        keyStatus.setAttribute("data-save-state", state);
        keyStatus.style.display = message ? "inline" : "none";
    }

    if (keyForm) {
        keyForm.addEventListener("submit", function (e) {
            e.preventDefault();
            const input = keyForm.querySelector('input[name="service_key"]');
            const submit = keyForm.querySelector('button[type="submit"]');
            const body = new URLSearchParams();
            body.append("service_key", input.value.trim());
            submit.disabled = true;
            showKeyStatus("saving", "Checking with HubSpot…");
            postJson("/admin/hubspot/key", body).then(function (result) {
                // Clear the field either way -- the key shouldn't linger in the page.
                input.value = "";
                submit.disabled = false;
                if (!result.ok) {
                    showKeyStatus("error", result.data.error || "Could not save the key.");
                    return;
                }
                showKeyStatus(result.data.warning ? "error" : "ok",
                    result.data.warning ? "Saved, but: " + result.data.warning : "Saved -- first sync started");
                window.setTimeout(function () { window.location.reload(); }, result.data.warning ? 6000 : 1200);
            }).catch(function () {
                input.value = "";
                submit.disabled = false;
                showKeyStatus("error", "Network error -- please try again.");
            });
        });
    }

    if (keyRemove) {
        keyRemove.addEventListener("click", function () {
            if (keyRemove.dataset.confirming !== "true") {
                keyRemove.dataset.confirming = "true";
                keyRemove.textContent = "Click again to remove";
                return;
            }
            keyRemove.disabled = true;
            postJson("/admin/hubspot/key/delete").then(function (result) {
                if (result.ok) {
                    window.location.reload();
                    return;
                }
                keyRemove.disabled = false;
                showKeyStatus("error", result.data.error || "Could not remove the key.");
            });
        });
    }

    // A sync already in progress (e.g. the 15-minute automatic one)
    // when the page opened -- keep the panel live until it finishes.
    if (root.getAttribute("data-latest-run-status") === "running") {
        baselineRunId -= 1;
        poll();
    }
});
