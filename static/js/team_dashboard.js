document.addEventListener("DOMContentLoaded", function () {
    var periodSelect = document.getElementById("period-select");
    var customDates = document.getElementById("custom-dates");

    if (periodSelect) {
        periodSelect.addEventListener("change", function () {
            if (customDates) {
                customDates.style.display = periodSelect.value === "custom" ? "flex" : "none";
            }
            if (periodSelect.value !== "custom") {
                periodSelect.form.submit();
            }
        });
    }

    var searchInput = document.getElementById("rep-search");
    if (searchInput) {
        var debounceTimer = null;
        searchInput.addEventListener("input", function () {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () {
                searchInput.form.submit();
            }, 450);
        });
    }

    document.querySelectorAll(".td-row-link").forEach(function (row) {
        row.addEventListener("click", function () {
            var href = row.getAttribute("data-href");
            if (href) {
                window.location = href;
            }
        });
    });

    // Rep name links inside leaderboard rows navigate to the Rep Profile;
    // stop the click from also bubbling to the row's own select-this-rep
    // handler above.
    document.querySelectorAll(".td-rep-link").forEach(function (link) {
        link.addEventListener("click", function (e) {
            e.stopPropagation();
        });
    });

    // Rep Profile tabs (Overview / Needs Attention) -- client-side toggle,
    // no reload.
    var tabButtons = document.querySelectorAll("[data-tab]");
    var tabPanels = document.querySelectorAll("[data-tab-panel]");
    if (tabButtons.length && tabPanels.length) {
        tabButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                var target = button.getAttribute("data-tab");

                tabButtons.forEach(function (b) {
                    b.classList.toggle("td-tab-active", b === button);
                });
                tabPanels.forEach(function (panel) {
                    panel.style.display = panel.getAttribute("data-tab-panel") === target ? "" : "none";
                });
            });
        });
    }
});
