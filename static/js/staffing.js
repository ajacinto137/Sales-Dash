// Staffing calendar (templates/staffing.html). Every action is a plain
// POST form; this only adds a click-twice confirm on deletes (same as the
// HubSpot key Remove button -- no native confirm() dialog) and stops
// double submits.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".td-staff-panel form, .td-staff-picker, .td-staff-open form").forEach(function (form) {
        const button = form.querySelector('button[type="submit"]');
        form.addEventListener("submit", function (e) {
            if (form.hasAttribute("data-confirm-twice") && form.dataset.confirming !== "true") {
                e.preventDefault();
                form.dataset.confirming = "true";
                button.dataset.label = button.textContent;
                button.textContent = "Click again to confirm";
                window.setTimeout(function () {
                    form.dataset.confirming = "";
                    button.textContent = button.dataset.label;
                }, 4000);
                return;
            }
            if (button) button.disabled = true;
        });
    });

    // Keep the selected day's details in view on small screens, where the
    // panel stacks below the calendar.
    if (window.location.search.indexOf("day=") !== -1 && !window.location.hash && window.matchMedia("(max-width: 980px)").matches) {
        const panel = document.getElementById("day");
        if (panel) panel.scrollIntoView({ block: "start" });
    }
});
