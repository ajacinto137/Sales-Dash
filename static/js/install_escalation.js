// Install Escalation (Tools menu, added 2026-10-26) -- filters + Mark
// Called / Undo for templates/install_escalation.html. Marking is a small
// fetch() POST to install_escalation_mark() in app.py; the row updates in
// place (no reload) so a rep keeps their place and filters in the list.
(function () {
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-esc-row]"));
    if (!rows.length) return;

    var stateSelect = document.querySelector("[data-esc-state]");
    var hideCalled = document.querySelector("[data-esc-hide-called]");
    var searchInput = document.querySelector("[data-esc-search]");
    var noneEl = document.querySelector("[data-esc-none]");

    function applyFilters() {
        var state = stateSelect ? stateSelect.value : "";
        var hide = hideCalled && hideCalled.checked;
        var terms = (searchInput ? searchInput.value : "").toLowerCase().split(/\s+/).filter(Boolean);
        var shown = 0;
        rows.forEach(function (row) {
            var haystack = row.getAttribute("data-search") || "";
            var visible = (!state || row.getAttribute("data-state") === state)
                && !(hide && row.hasAttribute("data-called"))
                && terms.every(function (t) { return haystack.indexOf(t) !== -1; });
            row.hidden = !visible;
            if (visible) shown += 1;
        });
        if (noneEl) noneEl.hidden = shown > 0;
    }

    function updateCounts() {
        var called = rows.filter(function (r) { return r.hasAttribute("data-called"); });
        var moved = called.filter(function (r) { return r.getAttribute("data-outcome") === "moved_up"; });
        var counts = { to_call: rows.length - called.length, called: called.length, moved_up: moved.length };
        Object.keys(counts).forEach(function (key) {
            var el = document.querySelector('[data-esc-count="' + key + '"]');
            if (el) el.textContent = counts[key].toLocaleString();
        });
    }

    function render(row, mark) {
        var q = function (sel) { return row.querySelector(sel); };
        q("[data-esc-pill-open]").hidden = !!mark;
        q("[data-esc-pill-done]").hidden = !mark;
        q("[data-esc-form]").hidden = !!mark;
        q("[data-esc-undo]").hidden = !(mark && mark.can_undo);
        q("[data-esc-outcome]").textContent = mark ? mark.outcome_label : "";
        q("[data-esc-by]").textContent = mark ? mark.called_by_name + " · " + mark.called_time_display : "";
        q("[data-esc-note]").textContent = mark && mark.note ? mark.note : "";
        q("[data-esc-status]").setAttribute("data-sort-value", mark ? "1" : "0");
        row.classList.toggle("td-esc-called", !!mark);
        if (mark) {
            row.setAttribute("data-called", "");
            row.setAttribute("data-outcome", mark.outcome);
        } else {
            row.removeAttribute("data-called");
            row.removeAttribute("data-outcome");
            q("[data-esc-outcome-select]").value = "";
            q("[data-esc-note-input]").value = "";
        }
        updateCounts();
        applyFilters();
    }

    function post(row, body, button) {
        var errorEl = row.querySelector("[data-esc-error]");
        errorEl.hidden = true;
        button.disabled = true;
        fetch("/tools/install-escalation/" + row.getAttribute("data-order-id") + "/call", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        })
            .then(function (resp) { return resp.json(); })
            .then(function (result) {
                if (!result.ok) throw new Error(result.error || "Could not save.");
                render(row, result.mark);
            })
            .catch(function (err) {
                errorEl.textContent = err.message || "Could not save.";
                errorEl.hidden = false;
            })
            .then(function () { button.disabled = false; });
    }

    rows.forEach(function (row) {
        var outcome = row.querySelector("[data-esc-outcome-select]");
        var note = row.querySelector("[data-esc-note-input]");
        var markBtn = row.querySelector("[data-esc-mark]");
        var undoBtn = row.querySelector("[data-esc-undo]");

        markBtn.addEventListener("click", function () {
            var errorEl = row.querySelector("[data-esc-error]");
            if (!outcome.value) {
                errorEl.textContent = "Pick how the call went.";
                errorEl.hidden = false;
                outcome.focus();
                return;
            }
            post(row, { outcome: outcome.value, note: note.value }, markBtn);
        });
        note.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); markBtn.click(); }
        });
        undoBtn.addEventListener("click", function () { post(row, { undo: true }, undoBtn); });
    });

    [stateSelect, hideCalled].forEach(function (el) { if (el) el.addEventListener("change", applyFilters); });
    if (searchInput) searchInput.addEventListener("input", applyFilters);
})();
