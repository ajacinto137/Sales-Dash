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
});
