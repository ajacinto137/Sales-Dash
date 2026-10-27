// Passings & Leads (templates/passings.html): Quick Actions (Log Call/
// Log Email/Log Visit/+ Note, spec #21) and the Global Time Range
// control's custom-date show/hide. Plain vanilla JS, no bundler -- same
// convention as attention.js, which this file's quick-action logic
// mirrors closely (small fetch() POST + in-place DOM update across BOTH
// the Cards and Table representations of a row, keyed by data-place-id,
// exactly like attention.js keys off data-sale-id).
//
// Reads/writes go through POST /passings/activity (see app.py's
// passings_log_activity()) via fetch(), updating every element sharing
// that place_id on success so a rep can log activity without a page
// reload and without losing their current filter/scroll position.
document.addEventListener("DOMContentLoaded", function () {
    // ---- Global Time Range: show/hide the custom date inputs, same
    // pattern team_dashboard.js's period-select/custom-dates already use
    // for the main Dashboard, under Passings-specific IDs so this stays
    // self-contained rather than editing that shared, unrelated file. ----
    var timeRangeSelect = document.getElementById("passings-time-range-select");
    var customDates = document.getElementById("passings-custom-dates");
    if (timeRangeSelect) {
        timeRangeSelect.addEventListener("change", function () {
            if (customDates) {
                customDates.style.display = timeRangeSelect.value === "custom" ? "flex" : "none";
            }
            if (timeRangeSelect.value !== "custom") {
                timeRangeSelect.form.submit();
            }
        });
    }

    // ---- Quick Actions ----
    var cards = document.querySelectorAll("[data-passing-card]");
    if (!cards.length) return;

    // "Residential Address" / "Undo Residential": saves the mark, then
    // reloads so the card leaves (or rejoins) the commercial lists and
    // every KPI/chart count reflects it.
    document.querySelectorAll("[data-mark-residential]").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var card = btn.closest("[data-passing-card]");
            var errorEl = card.querySelector("[data-mark-residential-error]");
            btn.disabled = true;
            fetch("/passings/residential", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    place_id: card.getAttribute("data-place-id"),
                    residential: btn.getAttribute("data-mark-residential") === "1",
                }),
            })
                .then(function (resp) { return resp.json(); })
                .then(function (result) {
                    if (!result.ok) throw new Error(result.error || "Could not save.");
                    window.location.reload();
                })
                .catch(function (err) {
                    btn.disabled = false;
                    if (errorEl) {
                        errorEl.textContent = err.message || "Could not save.";
                        errorEl.style.display = "block";
                    }
                });
        });
    });

    function formatNow() {
        // Best-effort immediate display before the server's own
        // Eastern-formatted timestamp round-trips back -- overwritten by
        // the real `activity_at_display` from the response a moment later.
        return "just now";
    }

    function applyActivityUpdate(placeId, data) {
        var last = data.last_activity;
        document.querySelectorAll('[data-passing-card][data-place-id="' + placeId + '"]').forEach(function (card) {
            var badgeSlot = card.querySelector("[data-activity-badge]");
            if (badgeSlot) {
                var badgeClass = last.activity_type === "Field Visit" ? "td-passing-badge-visited" : "td-passing-badge-called";
                var badgeText = last.activity_type === "Field Visit" ? "Visited" : (last.activity_type === "Call" ? "Called" : last.activity_type);
                badgeSlot.innerHTML = "";
                var span = document.createElement("span");
                span.className = "td-passing-badge " + badgeClass;
                span.textContent = badgeText;
                badgeSlot.appendChild(span);
            }
            var row = card.querySelector("[data-last-activity-row]");
            var text = card.querySelector("[data-last-activity-text]");
            if (row) row.style.display = "";
            if (text) {
                text.textContent = last.activity_type + " by " + (last.rep_display_name || "Unknown") + " · " + (last.activity_at_display || formatNow());
            }
        });

        document.querySelectorAll('[data-passing-row][data-place-id="' + placeId + '"]').forEach(function (row) {
            var cell = row.querySelector("[data-last-activity-cell]");
            var typeCell = row.querySelector("[data-activity-type-cell]");
            var repCell = row.querySelector("[data-activity-rep-cell]");
            if (cell) cell.textContent = last.activity_at_display || formatNow();
            if (typeCell) typeCell.textContent = last.activity_type;
            if (repCell) repCell.textContent = last.rep_display_name || "—";
        });
    }

    function logActivity(placeId, activityType, note, button) {
        if (button) button.disabled = true;
        return fetch("/passings/activity", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ place_id: placeId, activity_type: activityType, note: note || "" }),
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, data: data };
                });
            })
            .then(function (result) {
                if (button) button.disabled = false;
                if (!result.ok || !result.data.ok) {
                    return { ok: false, error: (result.data && result.data.error) || "Something went wrong. Please try again." };
                }
                applyActivityUpdate(placeId, result.data);
                return { ok: true };
            })
            .catch(function () {
                if (button) button.disabled = false;
                return { ok: false, error: "Network error -- please try again." };
            });
    }

    cards.forEach(function (card) {
        var placeId = card.getAttribute("data-place-id");

        card.querySelectorAll("[data-log-activity]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                logActivity(placeId, btn.getAttribute("data-log-activity"), "", btn);
            });
        });

        var openBtn = card.querySelector("[data-open-note-panel]");
        var panel = card.querySelector("[data-note-panel]");
        if (openBtn && panel) {
            openBtn.addEventListener("click", function () {
                panel.style.display = panel.style.display === "none" ? "flex" : "none";
            });
        }

        var cancelBtn = card.querySelector("[data-note-cancel]");
        if (cancelBtn && panel) {
            cancelBtn.addEventListener("click", function () {
                panel.style.display = "none";
            });
        }

        var saveBtn = card.querySelector("[data-note-save]");
        if (saveBtn && panel) {
            saveBtn.addEventListener("click", function () {
                var typeSelect = panel.querySelector("[data-note-activity-type]");
                var noteInput = panel.querySelector("[data-note-input]");
                var errorEl = panel.querySelector("[data-note-error]");
                var activityType = typeSelect ? typeSelect.value : "";
                var note = noteInput ? noteInput.value.trim() : "";

                if (errorEl) {
                    errorEl.style.display = "none";
                    errorEl.textContent = "";
                }

                logActivity(placeId, activityType, note, saveBtn).then(function (result) {
                    if (!result.ok) {
                        if (errorEl) {
                            errorEl.textContent = result.error;
                            errorEl.style.display = "block";
                        }
                        return;
                    }
                    if (noteInput) noteInput.value = "";
                    panel.style.display = "none";
                });
            });
        }
    });
    function postJson(url, body) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
                if (!res.ok || !data.ok) throw new Error(data.error || "Something went wrong. Please try again.");
                return data;
            });
        });
    }

    function showError(el, message) {
        if (!el) return;
        el.textContent = message;
        el.style.display = message ? "block" : "none";
    }

    // ---- Check Out (2026-10-27): tick cards (or table rows), pick a
    // rep, Check Out. Cards and table rows share data-place-id, so a box
    // ticked in one view is ticked in the other. Reloads afterwards so the
    // "Checked out to" badges show. ----
    var checkoutBar = document.querySelector("[data-checkout-bar]");
    if (checkoutBar) {
        var boxes = document.querySelectorAll("[data-checkout-select]");
        var selectAll = checkoutBar.querySelector("[data-checkout-select-all]");
        var countEl = checkoutBar.querySelector("[data-checkout-count]");
        var repSelect = checkoutBar.querySelector("[data-checkout-rep]");
        var submitBtn = checkoutBar.querySelector("[data-checkout-submit]");
        var messageEl = checkoutBar.querySelector("[data-checkout-message]");

        var selectedIds = function () {
            var ids = {};
            boxes.forEach(function (box) { if (box.checked) ids[box.value] = true; });
            return Object.keys(ids);
        };
        var refresh = function () {
            var n = selectedIds().length;
            countEl.textContent = n + " selected";
            submitBtn.disabled = n === 0 || !repSelect.value;
            var all = boxes.length > 0 && Array.prototype.every.call(boxes, function (b) { return b.checked; });
            selectAll.checked = all;
        };
        boxes.forEach(function (box) {
            box.addEventListener("change", function () {
                document.querySelectorAll('[data-checkout-select][value="' + box.value + '"]').forEach(function (other) {
                    other.checked = box.checked;
                });
                refresh();
            });
        });
        selectAll.addEventListener("change", function () {
            boxes.forEach(function (box) { box.checked = selectAll.checked; });
            refresh();
        });
        repSelect.addEventListener("change", refresh);
        submitBtn.addEventListener("click", function () {
            var ids = selectedIds();
            if (!ids.length || !repSelect.value) return;
            submitBtn.disabled = true;
            showError(messageEl, "");
            postJson("/passings/checkout", { place_ids: ids, sales_rep_id: repSelect.value })
                .then(function () { window.location.reload(); })
                .catch(function (err) {
                    showError(messageEl, err.message);
                    refresh();
                });
        });
        refresh();
    }

    // ---- Rep Profile "Checked Out" tab: bucket chips filter the cards,
    // each card's Bucket dropdown moves it, Return takes it off the tab.
    // Buckets belong to this profile only. ----
    var checkedOut = document.querySelector("[data-checked-out]");
    if (!checkedOut) return;
    var repBase = "/dashboard/reps/" + encodeURIComponent(checkedOut.getAttribute("data-rep-name")) + "/checkout-buckets";
    var bucketError = checkedOut.querySelector("[data-bucket-error]");
    var emptyEl = checkedOut.querySelector("[data-bucket-empty]");
    var activeFilter = "all";

    function applyBuckets() {
        var counts = { all: 0 };
        var shown = 0;
        checkedOut.querySelectorAll("[data-passing-card]").forEach(function (card) {
            var bucket = card.getAttribute("data-bucket-id") || "";
            counts.all += 1;
            counts[bucket] = (counts[bucket] || 0) + 1;
            var visible = activeFilter === "all" || activeFilter === bucket;
            card.style.display = visible ? "" : "none";
            if (visible) shown += 1;
        });
        checkedOut.querySelectorAll("[data-bucket-count]").forEach(function (el) {
            el.textContent = "(" + (counts[el.getAttribute("data-bucket-count")] || 0) + ")";
        });
        var total = document.querySelector("[data-checked-out-total]");
        if (total) total.textContent = counts.all.toLocaleString();
        if (emptyEl) emptyEl.style.display = shown ? "none" : "block";
    }

    checkedOut.querySelectorAll("[data-bucket-filter]").forEach(function (chip) {
        chip.addEventListener("click", function () {
            activeFilter = chip.getAttribute("data-bucket-filter");
            checkedOut.querySelectorAll("[data-bucket-filter]").forEach(function (c) {
                c.classList.toggle("td-checkout-chip-active", c === chip);
            });
            applyBuckets();
        });
    });

    var createForm = checkedOut.querySelector("[data-bucket-create]");
    if (createForm) {
        createForm.addEventListener("submit", function (e) {
            e.preventDefault();
            var input = createForm.querySelector("[data-bucket-name]");
            showError(bucketError, "");
            postJson(repBase, { name: input.value })
                .then(function () { window.location.hash = "checked-out"; window.location.reload(); })
                .catch(function (err) { showError(bucketError, err.message); });
        });
    }

    checkedOut.querySelectorAll("[data-bucket-delete]").forEach(function (btn) {
        btn.addEventListener("click", function () {
            showError(bucketError, "");
            postJson(repBase + "/" + btn.getAttribute("data-bucket-delete") + "/delete", {})
                .then(function () { window.location.hash = "checked-out"; window.location.reload(); })
                .catch(function (err) { showError(bucketError, err.message); });
        });
    });

    checkedOut.querySelectorAll("[data-passing-card]").forEach(function (card) {
        var placeId = card.getAttribute("data-place-id");
        var select = card.querySelector("[data-checkout-bucket-select]");
        if (select) {
            select.addEventListener("change", function () {
                var previous = card.getAttribute("data-bucket-id") || "";
                select.disabled = true;
                showError(bucketError, "");
                postJson(repBase + "/assign", { place_id: placeId, bucket_id: select.value || null })
                    .then(function () {
                        card.setAttribute("data-bucket-id", select.value);
                        applyBuckets();
                    })
                    .catch(function (err) {
                        select.value = previous;
                        showError(bucketError, err.message);
                    })
                    .then(function () { select.disabled = false; });
            });
        }
        var returnBtn = card.querySelector("[data-checkout-return]");
        if (returnBtn) {
            returnBtn.addEventListener("click", function () {
                returnBtn.disabled = true;
                showError(bucketError, "");
                postJson("/passings/checkout/return", { place_id: placeId })
                    .then(function () {
                        card.remove();
                        applyBuckets();
                    })
                    .catch(function (err) {
                        returnBtn.disabled = false;
                        showError(bucketError, err.message);
                    });
            });
        }
    });

    applyBuckets();
});
