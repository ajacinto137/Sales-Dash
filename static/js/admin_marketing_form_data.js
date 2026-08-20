// Admin Portal -- Marketing Form Data (templates/admin/marketing_form_data.html).
// This page is plain server-rendered GET navigation (search/filters/
// pagination/refresh are all just links or a GET form to the same route,
// re-querying PlanetWeb fresh every time -- see admin_marketing_form_data()
// in app.py) -- no fetch/AJAX involved. This script only adds an
// immediate "Refreshing…" loading state on the Refresh Data link and the
// filter form's Apply button, since the actual page navigation itself
// isn't instant against a live SQL Server query.
document.addEventListener("DOMContentLoaded", function () {
    var refreshBtn = document.querySelector("[data-refresh-btn]");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", function () {
            refreshBtn.textContent = "Refreshing…";
            refreshBtn.setAttribute("aria-busy", "true");
            refreshBtn.style.pointerEvents = "none";
        });
    }

    var filterForm = document.querySelector(".td-marketing-filters");
    if (filterForm) {
        filterForm.addEventListener("submit", function () {
            var applyBtn = filterForm.querySelector('button[type="submit"]');
            if (applyBtn) {
                applyBtn.disabled = true;
                applyBtn.textContent = "Applying…";
            }
        });
    }
});
