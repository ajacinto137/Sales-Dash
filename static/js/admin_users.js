// Admin Portal -- User Management table (templates/admin/users.html).
// Inline edit (email/role/Sales Rep mapping) and row actions
// (disable/enable, send setup/reset email) via fetch to the
// /admin/users/<id>* routes in app.py -- no full page reload for any of
// these, same fetch/update-in-place idiom as attention.js/search.js.
// Server-side is authoritative for every one of these (role/status
// checks happen in app.py's @auth.admin_required + user_store.py), this
// script only reflects what the server already allowed.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-user-row]").forEach(function (row) {
        var userId = row.getAttribute("data-user-id");
        var editBtn = row.querySelector('[data-action="edit"]');
        var saveBtn = row.querySelector('[data-action="save"]');
        var cancelBtn = row.querySelector('[data-action="cancel"]');
        var errorEl = row.querySelector("[data-row-error]");
        var fields = row.querySelectorAll("[data-field-input]");
        var displays = row.querySelectorAll("[data-field-display]");

        function showError(message) {
            errorEl.textContent = message;
            errorEl.style.display = "block";
        }

        function clearError() {
            errorEl.textContent = "";
            errorEl.style.display = "none";
        }

        function setEditing(editing) {
            fields.forEach(function (el) { el.style.display = editing ? "" : "none"; });
            displays.forEach(function (el) { el.style.display = editing ? "none" : ""; });
            editBtn.style.display = editing ? "none" : "";
            saveBtn.style.display = editing ? "" : "none";
            cancelBtn.style.display = editing ? "" : "none";
            clearError();
        }

        if (editBtn) {
            editBtn.addEventListener("click", function () { setEditing(true); });
        }
        if (cancelBtn) {
            cancelBtn.addEventListener("click", function () {
                setEditing(false);
                window.location.reload();
            });
        }

        if (saveBtn) {
            saveBtn.addEventListener("click", function () {
                var body = new URLSearchParams();
                fields.forEach(function (el) {
                    body.append(el.getAttribute("data-field-input"), el.value);
                });
                saveBtn.disabled = true;
                fetch("/admin/users/" + userId, { method: "POST", body: body })
                    .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
                    .then(function (result) {
                        saveBtn.disabled = false;
                        if (!result.ok || !result.data.ok) {
                            showError((result.data && result.data.error) || "Could not save changes.");
                            return;
                        }
                        window.location.reload();
                    })
                    .catch(function () {
                        saveBtn.disabled = false;
                        showError("Network error -- please try again.");
                    });
            });
        }

        ["disable", "enable", "send-setup", "send-reset"].forEach(function (action) {
            var btn = row.querySelector('[data-action="' + action + '"]');
            if (!btn) return;
            btn.addEventListener("click", function () {
                if (!window.confirm("Are you sure you want to " + action.replace("-", " ") + " for this user?")) {
                    return;
                }
                btn.disabled = true;
                fetch("/admin/users/" + userId + "/" + action, { method: "POST" })
                    .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
                    .then(function (result) {
                        btn.disabled = false;
                        if (!result.ok || !result.data.ok) {
                            showError((result.data && result.data.error) || "Action failed.");
                            return;
                        }
                        window.location.reload();
                    })
                    .catch(function () {
                        btn.disabled = false;
                        showError("Network error -- please try again.");
                    });
            });
        });
    });
});
