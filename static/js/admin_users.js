// Admin Portal -- User Management table (templates/admin/users.html).
// Inline edit (email/role/Sales Rep mapping) and row actions
// (disable/enable, send setup/reset email) via fetch to the
// /admin/users/<id>* routes in app.py -- no full page reload for any of
// these, same fetch/update-in-place idiom as attention.js/search.js.
// Server-side is authoritative for every one of these (role/status
// checks happen in app.py's @auth.admin_required + user_store.py), this
// script only reflects what the server already allowed.
document.addEventListener("DOMContentLoaded", function () {
    // ---- New User: manual single-user creation, alongside the Excel
    // import flow -- posts straight to /admin/users/create
    // (user_store.create_user()), same no-auto-email/status=pending
    // result as an imported row. ----
    var newUserPanel = document.querySelector("[data-new-user-panel]");
    var newUserOpenBtn = document.querySelector('[data-action="new-user-open"]');
    var newUserCancelBtn = document.querySelector('[data-action="new-user-cancel"]');
    var newUserSaveBtn = document.querySelector('[data-action="new-user-save"]');
    var newUserError = document.querySelector("[data-new-user-error]");
    var newUserInputs = document.querySelectorAll("[data-new-user-input]");

    function showNewUserError(message) {
        newUserError.textContent = message;
        newUserError.style.display = "block";
    }

    function clearNewUserError() {
        newUserError.textContent = "";
        newUserError.style.display = "none";
    }

    function resetNewUserForm() {
        newUserInputs.forEach(function (el) { el.value = ""; });
        clearNewUserError();
    }

    if (newUserOpenBtn) {
        newUserOpenBtn.addEventListener("click", function () {
            newUserPanel.style.display = newUserPanel.style.display === "none" ? "flex" : "none";
        });
    }
    if (newUserCancelBtn) {
        newUserCancelBtn.addEventListener("click", function () {
            newUserPanel.style.display = "none";
            resetNewUserForm();
        });
    }
    if (newUserSaveBtn) {
        newUserSaveBtn.addEventListener("click", function () {
            clearNewUserError();
            var body = new URLSearchParams();
            newUserInputs.forEach(function (el) {
                body.append(el.getAttribute("data-new-user-input"), el.value);
            });
            newUserSaveBtn.disabled = true;
            fetch("/admin/users/create", { method: "POST", body: body })
                .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
                .then(function (result) {
                    newUserSaveBtn.disabled = false;
                    if (!result.ok || !result.data.ok) {
                        showNewUserError((result.data && result.data.error) || "Could not create user.");
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    newUserSaveBtn.disabled = false;
                    showNewUserError("Network error -- please try again.");
                });
        });
    }

    // ---- Actions dropdown: collapses the previous per-row button strip
    // (Edit/Disable/Send Setup/Set Password all inline) into one menu.
    // Only one row's menu is open at a time; clicking any item or
    // anywhere outside closes it. ----
    function closeAllActionsMenus(except) {
        document.querySelectorAll(".td-admin-actions-menu.td-admin-actions-open").forEach(function (menu) {
            if (menu !== except) menu.classList.remove("td-admin-actions-open");
        });
    }

    document.addEventListener("click", function (e) {
        if (!e.target.closest(".td-admin-actions-menu")) closeAllActionsMenus();
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeAllActionsMenus();
    });

    document.querySelectorAll("[data-user-row]").forEach(function (row) {
        var userId = row.getAttribute("data-user-id");
        var actionsMenu = row.querySelector("[data-actions-menu]");
        var actionsToggle = row.querySelector('[data-action="actions-toggle"]');
        var editBtn = row.querySelector('[data-action="edit"]');
        var saveBtn = row.querySelector('[data-action="save"]');
        var cancelBtn = row.querySelector('[data-action="cancel"]');
        var errorEl = row.querySelector("[data-row-error]");
        var fields = row.querySelectorAll("[data-field-input]");
        var displays = row.querySelectorAll("[data-field-display]");

        if (actionsToggle) {
            actionsToggle.addEventListener("click", function (e) {
                e.stopPropagation();
                var isOpen = actionsMenu.classList.contains("td-admin-actions-open");
                closeAllActionsMenus();
                actionsMenu.classList.toggle("td-admin-actions-open", !isOpen);
            });
        }
        // Any action inside the dropdown closes it -- the action's own
        // handler (below) still runs since this fires during the
        // capture-less bubble phase before the outside-click listener.
        actionsMenu.querySelectorAll(".td-admin-actions-item").forEach(function (item) {
            item.addEventListener("click", function () {
                actionsMenu.classList.remove("td-admin-actions-open");
            });
        });

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
            actionsMenu.style.display = editing ? "none" : "";
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

        // ---- Set Password: an Admin types a password directly and
        // hands it to the user out of band (phone/in person) -- bypasses
        // the emailed-link flow entirely, for when SMTP isn't configured
        // or an Admin just doesn't want to wait on email. ----
        var setPasswordPanel = row.querySelector("[data-set-password-panel]");
        var setPasswordInput = row.querySelector("[data-set-password-input]");
        var openBtn = row.querySelector('[data-action="set-password-open"]');
        var cancelSetPasswordBtn = row.querySelector('[data-action="set-password-cancel"]');
        var saveSetPasswordBtn = row.querySelector('[data-action="set-password-save"]');

        if (openBtn) {
            openBtn.addEventListener("click", function () {
                var isOpen = setPasswordPanel.style.display !== "none";
                setPasswordPanel.style.display = isOpen ? "none" : "flex";
                clearError();
                if (!isOpen) setPasswordInput.focus();
            });
        }
        if (cancelSetPasswordBtn) {
            cancelSetPasswordBtn.addEventListener("click", function () {
                setPasswordPanel.style.display = "none";
                setPasswordInput.value = "";
                clearError();
            });
        }
        if (saveSetPasswordBtn) {
            saveSetPasswordBtn.addEventListener("click", function () {
                var password = setPasswordInput.value;
                if (password.length < 8) {
                    showError("Password must be at least 8 characters.");
                    return;
                }
                saveSetPasswordBtn.disabled = true;
                fetch("/admin/users/" + userId + "/set-password", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password: password }),
                })
                    .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
                    .then(function (result) {
                        saveSetPasswordBtn.disabled = false;
                        if (!result.ok || !result.data.ok) {
                            showError((result.data && result.data.error) || "Could not set password.");
                            return;
                        }
                        window.location.reload();
                    })
                    .catch(function () {
                        saveSetPasswordBtn.disabled = false;
                        showError("Network error -- please try again.");
                    });
            });
        }
    });
});
