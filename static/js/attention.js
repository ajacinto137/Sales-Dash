// Needs Attention workflow: Attention Status + notes, layered onto the
// Cards view of the shared Bulk Account View (see
// templates/_attention_overview.html, templates/_attention_controls.html).
// This script is loaded on every page that can render a Bulk Account View
// (rep_profile.html, bulk_account_view.html) but only ever does anything
// on the needs_attention view -- every other view has no
// [data-attention-*] elements, so this whole file is a no-op there.
//
// Reads/writes go through /dashboard/attention/<sale_id>/status and
// /dashboard/attention/<sale_id>/notes (see app.py) via fetch(), updating
// the DOM in place on success so a rep can work through a dozens-strong
// Needs Attention list without a page reload. Note text from the server
// is always inserted via textContent, never innerHTML, since it's
// rep-authored freeform text.
document.addEventListener("DOMContentLoaded", function () {
    var blocks = document.querySelectorAll("[data-attention-block]");
    var overview = document.querySelector("[data-attention-overview]");
    if (!blocks.length && !overview) {
        return;
    }

    var AUTHOR_STORAGE_KEY = "td-attention-author";
    var root = document.querySelector("[data-bulk-view]") || document;

    // ---- Author name, shared across every card via localStorage ----
    var authorInput = document.querySelector("[data-attention-author]");
    if (authorInput) {
        var savedAuthor = window.localStorage.getItem(AUTHOR_STORAGE_KEY);
        if (savedAuthor) {
            authorInput.value = savedAuthor;
        }
        authorInput.addEventListener("input", function () {
            window.localStorage.setItem(AUTHOR_STORAGE_KEY, authorInput.value);
        });
    }

    function getAuthor() {
        return authorInput ? authorInput.value.trim() : "";
    }

    function statusToClass(status) {
        return status ? status.toLowerCase().replace(/ /g, "-") : "unclassified";
    }

    // ---- Progress bar / filter counts -- recomputed after every save.
    // Dedupes by data-sale-id since an account appears once in the Cards
    // view and once in the Table view; only one should count. ----
    function recomputeAttentionCounts() {
        if (!overview) return;
        var statuses = [];
        try {
            statuses = JSON.parse(overview.getAttribute("data-attention-statuses") || "[]");
        } catch (e) {
            statuses = [];
        }

        var counts = { all: 0, unclassified: 0, addressed: 0 };
        statuses.forEach(function (s) { counts[s] = 0; });

        var seen = {};
        root.querySelectorAll("[data-attention-status]").forEach(function (el) {
            var saleId = el.getAttribute("data-sale-id");
            if (!saleId || seen[saleId]) return;
            seen[saleId] = true;

            var status = el.getAttribute("data-attention-status");
            counts.all++;
            if (status) {
                counts.addressed++;
                counts[status] = (counts[status] || 0) + 1;
            } else {
                counts.unclassified++;
            }
        });

        Object.keys(counts).forEach(function (key) {
            document.querySelectorAll('[data-attention-count="' + key + '"]').forEach(function (el) {
                el.textContent = counts[key];
            });
        });

        var fill = document.querySelector("[data-attention-progress-fill]");
        var label = document.querySelector("[data-attention-progress-label]");
        var pct = counts.all ? Math.round((counts.addressed / counts.all) * 100) : 0;
        if (fill) fill.style.width = pct + "%";
        if (label) label.textContent = counts.addressed + " / " + counts.all + " Addressed";
    }

    // ---- Filtering: All / Unclassified / Addressed / per-status. Applies
    // to both the Cards and Table representations of each account (both
    // carry the same data-sale-id/data-attention-status), so switching
    // view modes keeps whatever filter is active. Display-only -- never
    // touches the server or the underlying Needs Attention population. ----
    document.querySelectorAll("[data-attention-filter]").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var group = btn.parentElement.querySelectorAll("[data-attention-filter]");
            group.forEach(function (b) {
                b.classList.toggle("td-attention-filter-active", b === btn);
            });

            var filterValue = btn.getAttribute("data-attention-filter");
            root.querySelectorAll("[data-attention-status]").forEach(function (el) {
                var status = el.getAttribute("data-attention-status");
                var match =
                    filterValue === "all" ? true :
                    filterValue === "unclassified" ? !status :
                    filterValue === "addressed" ? !!status :
                    status === filterValue;
                el.classList.toggle("td-attention-filtered-out", !match);
            });
        });
    });

    function closeAllPanels(block) {
        block.querySelectorAll("[data-attention-panel]").forEach(function (panel) {
            panel.style.display = "none";
        });
    }

    function buildHistoryItem(note) {
        var item = document.createElement("div");
        item.className = "td-attention-history-item";

        if (note.previous_status) {
            var change = document.createElement("div");
            change.className = "td-attention-history-change";
            change.textContent = "Attention Status changed from " + note.previous_status + " to " + note.attention_status;
            item.appendChild(change);
        }

        var meta = document.createElement("div");
        meta.className = "td-attention-history-meta";
        meta.textContent = note.created_at_display + (note.created_by ? " — " + note.created_by : "");
        item.appendChild(meta);

        var body = document.createElement("div");
        body.className = "td-attention-history-note";
        body.textContent = note.note;
        item.appendChild(body);

        return item;
    }

    function applySaveResult(block, data) {
        var status = data.attention_status || "";
        var notes = data.notes || [];

        // Keep the Cards card AND the Table row (if present) in sync --
        // both carry the same data-sale-id/data-attention-status pair
        // purely for filtering, even though only Cards has edit controls.
        root.querySelectorAll('[data-sale-id="' + data.sale_id + '"][data-attention-status]').forEach(function (el) {
            el.setAttribute("data-attention-status", status);
        });

        var badge = block.querySelector("[data-attention-badge]");
        if (badge) {
            badge.className = "td-attention-badge td-attention-badge-" + statusToClass(status);
            badge.textContent = status || "Unclassified";
        }

        var classifyBtn = block.querySelector('[data-attention-open="classify"]');
        if (classifyBtn) classifyBtn.textContent = status ? "Change Status" : "Classify";

        var noteBtn = block.querySelector('[data-attention-open="note"]');
        if (noteBtn) noteBtn.style.display = status ? "" : "none";

        var historyBtn = block.querySelector('[data-attention-open="history"]');
        if (historyBtn) historyBtn.style.display = notes.length ? "" : "none";

        var notesCountEl = block.querySelector("[data-attention-notes-count]");
        if (notesCountEl) notesCountEl.textContent = notes.length;

        var statusSelect = block.querySelector("[data-attention-status-input]");
        if (statusSelect) statusSelect.value = status;

        var historyList = block.querySelector("[data-attention-history-list]");
        if (historyList) {
            historyList.textContent = "";
            notes.forEach(function (note) {
                historyList.appendChild(buildHistoryItem(note));
            });
        }
    }

    blocks.forEach(function (block) {
        var card = block.closest("[data-sale-id]");
        var saleId = card ? card.getAttribute("data-sale-id") : null;

        block.querySelectorAll("[data-attention-open]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var target = btn.getAttribute("data-attention-open");
                var panel = block.querySelector('[data-attention-panel="' + target + '"]');
                if (!panel) return;
                var isOpen = panel.style.display !== "none";
                closeAllPanels(block);
                panel.style.display = isOpen ? "none" : "flex";
            });
        });

        block.querySelectorAll("[data-attention-cancel]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                closeAllPanels(block);
            });
        });

        block.querySelectorAll("[data-attention-save]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                if (!saleId) return;

                var kind = btn.getAttribute("data-attention-save"); // "status" or "note"
                var panel = btn.closest("[data-attention-panel]");
                var errorEl = panel.querySelector("[data-attention-error]");
                var noteInput = panel.querySelector("[data-attention-note-input]");
                var note = noteInput ? noteInput.value.trim() : "";

                errorEl.style.display = "none";
                errorEl.textContent = "";

                if (!note) {
                    errorEl.textContent = "A note is required.";
                    errorEl.style.display = "block";
                    return;
                }

                var url;
                var body;
                if (kind === "status") {
                    var statusInput = panel.querySelector("[data-attention-status-input]");
                    var status = statusInput ? statusInput.value : "";
                    if (!status) {
                        errorEl.textContent = "Select an Attention Status.";
                        errorEl.style.display = "block";
                        return;
                    }
                    url = "/dashboard/attention/" + saleId + "/status";
                    body = { status: status, note: note, author: getAuthor() };
                } else {
                    url = "/dashboard/attention/" + saleId + "/notes";
                    body = { note: note, author: getAuthor() };
                }

                btn.disabled = true;
                fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                })
                    .then(function (res) {
                        return res.json().then(function (data) {
                            return { ok: res.ok, data: data };
                        });
                    })
                    .then(function (result) {
                        btn.disabled = false;
                        if (!result.ok || !result.data.ok) {
                            errorEl.textContent = (result.data && result.data.error) || "Something went wrong. Please try again.";
                            errorEl.style.display = "block";
                            return;
                        }
                        applySaveResult(block, result.data);
                        if (noteInput) noteInput.value = "";
                        closeAllPanels(block);
                        recomputeAttentionCounts();
                    })
                    .catch(function () {
                        btn.disabled = false;
                        errorEl.textContent = "Network error -- please try again.";
                        errorEl.style.display = "block";
                    });
            });
        });
    });
});
