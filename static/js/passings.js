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
});
