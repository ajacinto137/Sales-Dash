// Admin Portal -- External Rep Sales (templates/admin/external_rep_sales.html).
// Search/filters/sorting/pagination are plain GET navigation (server-side,
// see external_sales_store.query_sales()). This script adds:
//   - "Refresh Data": POST /admin/external-rep-sales/sync, then polls
//     /admin/external-rep-sales/status until the run finishes and reloads.
//   - Column show/hide, remembered per browser (localStorage, best effort).
//   - Rep Mapping row saves (POST /admin/external-rep-sales/reps/<id>).
// Server-side @auth.admin_required is the real access control.
document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("external-rep-sales");
    if (!root) return;

    function postForm(url, body) {
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

    function showStatus(el, state, message) {
        el.textContent = message;
        el.setAttribute("data-save-state", state);
        el.style.display = message ? "inline" : "none";
    }

    // ---------------- Refresh Data ----------------

    const syncBtn = root.querySelector("[data-ext-sync]");
    const syncStatus = root.querySelector("[data-ext-sync-status]");
    const errorEl = root.querySelector("[data-ext-error]");
    let baselineRunId = parseInt(root.getAttribute("data-latest-run-id") || "0", 10);
    let waitingForRun = false;
    let emptyPolls = 0;
    let pollTimer = null;

    function set(key, value) {
        root.querySelectorAll('[data-ext="' + key + '"]').forEach(function (el) { el.textContent = value; });
    }

    function poll() {
        fetch("/admin/external-rep-sales/status", { headers: { Accept: "application/json" } })
            .then(function (res) { return res.json(); })
            .then(function (s) {
                set("last_success_at", fmtTime(s.last_success_at));
                const run = s.latest_run;
                const running = run && run.status === "running";
                if (running) {
                    syncBtn.disabled = true;
                    showStatus(syncStatus, "saving", "Refreshing from PlanetWeb…");
                    pollTimer = window.setTimeout(poll, 2000);
                } else if (run && run.id > baselineRunId) {
                    if (run.status === "success") {
                        showStatus(syncStatus, "ok", "Refreshed");
                        window.location.reload();
                    } else {
                        syncBtn.disabled = false;
                        showStatus(syncStatus, "error", "Refresh failed");
                        set("last_error", s.last_error || run.error || "Unknown error");
                        errorEl.style.display = "block";
                        baselineRunId = run.id;
                    }
                } else if (waitingForRun && emptyPolls++ < 15) {
                    // The sync thread may not have recorded its run yet.
                    pollTimer = window.setTimeout(poll, 1500);
                } else {
                    syncBtn.disabled = false;
                    showStatus(syncStatus, "", "");
                }
            })
            .catch(function () { pollTimer = window.setTimeout(poll, 4000); });
    }

    syncBtn.addEventListener("click", function () {
        syncBtn.disabled = true;
        showStatus(syncStatus, "saving", "Starting refresh…");
        postForm("/admin/external-rep-sales/sync").then(function (result) {
            if (!result.ok && result.status !== 409) {
                syncBtn.disabled = false;
                showStatus(syncStatus, "error", result.data.error || "Could not start the refresh.");
                return;
            }
            // 409 = a refresh (e.g. the automatic one) is already running:
            // just follow it.
            waitingForRun = true;
            emptyPolls = 0;
            window.clearTimeout(pollTimer);
            poll();
        }).catch(function () {
            syncBtn.disabled = false;
            showStatus(syncStatus, "error", "Network error -- please try again.");
        });
    });

    if (root.getAttribute("data-latest-run-status") === "running") {
        baselineRunId -= 1;
        poll();
    }

    // ---------------- Filters ----------------

    const filterForm = document.querySelector("[data-ext-filters]");
    if (filterForm) {
        filterForm.addEventListener("submit", function () {
            const applyBtn = filterForm.querySelector('button[type="submit"]');
            if (applyBtn) {
                applyBtn.disabled = true;
                applyBtn.textContent = "Applying…";
            }
        });
        filterForm.querySelectorAll("[data-ext-autosubmit]").forEach(function (el) {
            el.addEventListener("change", function () { filterForm.requestSubmit(); });
        });
    }

    const perPage = document.querySelector("[data-ext-per-page]");
    if (perPage) {
        perPage.addEventListener("change", function () {
            const url = new URL(window.location.href);
            url.searchParams.set("per_page", perPage.value);
            url.searchParams.set("page", "1");
            window.location.href = url.toString();
        });
    }

    // ---------------- Column visibility ----------------

    const table = document.querySelector("[data-ext-table]");
    const picker = document.querySelector("[data-ext-columns]");
    const STORAGE_KEY = "externalRepSales.columns";

    function readSaved() {
        try {
            const raw = window.localStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (e) {
            return null;
        }
    }

    function save(visible) {
        try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(visible)); } catch (e) { /* ignore */ }
    }

    if (table && picker) {
        const boxes = Array.prototype.slice.call(picker.querySelectorAll('input[type="checkbox"]'));
        const countEl = picker.querySelector("[data-ext-visible-count]");

        function apply() {
            const visible = {};
            boxes.forEach(function (b) { visible[b.value] = b.checked; });
            table.querySelectorAll("[data-col]").forEach(function (cell) {
                cell.classList.toggle("td-ext-col-hidden", !visible[cell.getAttribute("data-col")]);
            });
            countEl.textContent = boxes.filter(function (b) { return b.checked; }).length + "/" + boxes.length;
            return visible;
        }

        const saved = readSaved();
        if (saved) {
            boxes.forEach(function (b) { if (b.value in saved) b.checked = !!saved[b.value]; });
        }
        apply();

        boxes.forEach(function (b) {
            b.addEventListener("change", function () {
                // Keep at least one column visible.
                if (!boxes.some(function (x) { return x.checked; })) b.checked = true;
                save(apply());
            });
        });
        picker.querySelector("[data-ext-columns-all]").addEventListener("click", function () {
            boxes.forEach(function (b) { b.checked = true; });
            save(apply());
        });
        picker.querySelector("[data-ext-columns-reset]").addEventListener("click", function () {
            boxes.forEach(function (b) { b.checked = b.getAttribute("data-default") === "1"; });
            try { window.localStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
            apply();
        });
        document.addEventListener("click", function (e) {
            if (picker.open && !picker.contains(e.target)) picker.open = false;
        });
    }

    // ---------------- Rep Mapping ----------------

    const mapRows = Array.prototype.slice.call(document.querySelectorAll("[data-ext-map-row]"));
    mapRows.forEach(function (row) {
        const saveBtn = row.querySelector("[data-ext-map-save]");
        const status = row.querySelector("[data-save-status]");
        const fields = row.querySelectorAll("input[name], select[name]");

        fields.forEach(function (f) {
            const evt = f.type === "checkbox" || f.tagName === "SELECT" ? "change" : "input";
            f.addEventListener(evt, function () {
                saveBtn.disabled = false;
                showStatus(status, "", "");
            });
            if (f.tagName === "INPUT" && f.type !== "checkbox") {
                f.addEventListener("keydown", function (e) {
                    if (e.key === "Enter" && !saveBtn.disabled) {
                        e.preventDefault();
                        saveBtn.click();
                    }
                });
            }
        });

        saveBtn.addEventListener("click", function () {
            const body = new URLSearchParams();
            fields.forEach(function (f) {
                if (f.type === "checkbox") {
                    if (f.checked) body.append(f.name, "1");
                } else {
                    body.append(f.name, f.value.trim());
                }
            });
            saveBtn.disabled = true;
            showStatus(status, "saving", "Saving…");
            postForm("/admin/external-rep-sales/reps/" + encodeURIComponent(row.getAttribute("data-rep-id")), body)
                .then(function (result) {
                    if (!result.ok) {
                        saveBtn.disabled = false;
                        showStatus(status, "error", result.data.error || "Could not save.");
                        return;
                    }
                    showStatus(status, "ok", "Saved");
                    const rep = row.querySelector('[name="rep_name"]').value.trim();
                    const org = row.querySelector('[name="organization"]').value.trim();
                    row.setAttribute("data-unmapped", rep || org ? "0" : "1");
                    const pill = row.querySelector(".td-pill-pending");
                    if (pill && (rep || org)) pill.remove();
                })
                .catch(function () {
                    saveBtn.disabled = false;
                    showStatus(status, "error", "Network error -- please try again.");
                });
        });
    });

    const mapSearch = document.querySelector("[data-ext-map-search]");
    const mapUnmapped = document.querySelector("[data-ext-map-unmapped]");
    function filterMapRows() {
        const term = (mapSearch.value || "").trim().toLowerCase();
        mapRows.forEach(function (row) {
            const matches = !term || row.getAttribute("data-search").indexOf(term) !== -1;
            const unmappedOk = !mapUnmapped.checked || row.getAttribute("data-unmapped") === "1";
            row.style.display = matches && unmappedOk ? "" : "none";
        });
    }
    if (mapSearch && mapUnmapped) {
        mapSearch.addEventListener("input", filterMapRows);
        mapUnmapped.addEventListener("change", filterMapRows);
    }
});
