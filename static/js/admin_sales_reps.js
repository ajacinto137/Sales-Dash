// Admin Portal -- Sales Reps table (templates/admin/sales_reps.html).
// One editable field per row (Team), so this auto-saves on change
// rather than reusing admin_users.js's Edit/Save/Cancel flow -- POSTs to
// /admin/sales-reps/<id>/team (user_store.update_sales_rep_team() in
// app.py) and updates the pill + a transient status message in place,
// no full page reload. Server-side is authoritative (role/status checks
// happen in app.py's @auth.admin_required + user_store.py), this script
// only reflects what the server already allowed -- same convention as
// admin_users.js.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-rep-row]").forEach(function (row) {
        var repId = row.getAttribute("data-rep-id");
        var select = row.querySelector("[data-team-select]");
        var pillWrap = row.querySelector("[data-team-pill]");
        var status = row.querySelector("[data-save-status]");
        if (!select) return;

        var lastSavedValue = select.value;

        function showStatus(state, message) {
            status.textContent = message;
            status.setAttribute("data-save-state", state);
            status.style.display = "inline";
        }

        function clearStatusSoon() {
            window.setTimeout(function () {
                status.style.display = "none";
            }, 2500);
        }

        function pillHtml(team) {
            if (!team) return '<span class="td-admin-muted">Unassigned</span>';
            var slug = team.toLowerCase().replace(/ /g, "-");
            var span = document.createElement("span");
            span.className = "td-pill td-pill-team-" + slug;
            span.textContent = team;
            return span.outerHTML;
        }

        select.addEventListener("change", function () {
            var team = select.value;
            select.disabled = true;
            showStatus("saving", "Saving…");

            var body = new URLSearchParams();
            body.append("team", team);

            fetch("/admin/sales-reps/" + repId + "/team", { method: "POST", body: body })
                .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
                .then(function (result) {
                    select.disabled = false;
                    if (!result.ok || !result.data.ok) {
                        select.value = lastSavedValue;
                        showStatus("error", (result.data && result.data.error) || "Could not save.");
                        clearStatusSoon();
                        return;
                    }
                    lastSavedValue = team;
                    pillWrap.innerHTML = pillHtml(team);
                    showStatus("ok", "Saved");
                    clearStatusSoon();
                })
                .catch(function () {
                    select.disabled = false;
                    select.value = lastSavedValue;
                    showStatus("error", "Network error -- please try again.");
                    clearStatusSoon();
                });
        });
    });
});
