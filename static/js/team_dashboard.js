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

    // Rep name links and metric-cell drill-down links inside leaderboard
    // rows navigate elsewhere; stop the click from also bubbling to the
    // row's own select-this-rep handler above.
    document.querySelectorAll(".td-rep-link, .td-metric-link").forEach(function (link) {
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

    // Bulk Account View: Cards / Table toggle -- scoped per [data-bulk-view]
    // instance so this still works if the partial is ever included more
    // than once on the same page (e.g. a future page with two lists).
    document.querySelectorAll("[data-bulk-view]").forEach(function (container) {
        var viewButtons = container.querySelectorAll("[data-bulk-view-btn]");
        var viewPanels = container.querySelectorAll("[data-bulk-view-panel]");
        viewButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                var target = button.getAttribute("data-bulk-view-btn");

                viewButtons.forEach(function (b) {
                    b.classList.toggle("td-chart-toggle-active", b === button);
                });
                viewPanels.forEach(function (panel) {
                    panel.style.display = panel.getAttribute("data-bulk-view-panel") === target ? "" : "none";
                });
            });
        });
    });

    // Bulk Account View's Table mode: click a column header to sort by it
    // (client-side -- every row is already on the page, no reload needed).
    // The table is a CSS Grid (see team_dashboard.css), not a <table> --
    // each data row is a .td-bulk-grid-row wrapper (display: contents) so
    // moving the wrapper with appendChild() moves its whole row of cells
    // together, exactly like the old tbody.appendChild(tr) approach. Each
    // cell's data-sort-value carries the raw comparable value (e.g.
    // "YYYY-MM-DD" for Scheduled, not the "MM/DD/YYYY" display text) so
    // sorting stays correct regardless of how the cell is formatted.
    document.querySelectorAll("[data-sortable-grid]").forEach(function (grid) {
        var headerCells = grid.querySelectorAll(".td-bulk-grid-head");
        var state = { key: null, dir: "asc" };

        function sortBy(headerCell, columnIndex) {
            var key = headerCell.getAttribute("data-sort-key");
            state.dir = (state.key === key && state.dir === "asc") ? "desc" : "asc";
            state.key = key;

            var rows = Array.from(grid.querySelectorAll(":scope > .td-bulk-grid-row"));
            rows.sort(function (rowA, rowB) {
                var cellsA = rowA.querySelectorAll(".td-bulk-grid-cell");
                var cellsB = rowB.querySelectorAll(".td-bulk-grid-cell");
                var valueA = cellsA[columnIndex].getAttribute("data-sort-value") || "";
                var valueB = cellsB[columnIndex].getAttribute("data-sort-value") || "";
                if (valueA === valueB) return 0;
                var comparison = valueA < valueB ? -1 : 1;
                return state.dir === "asc" ? comparison : -comparison;
            });
            rows.forEach(function (row) {
                grid.appendChild(row);
            });

            headerCells.forEach(function (h) {
                h.classList.remove("td-sort-active");
                var arrow = h.querySelector("[data-sort-arrow]");
                if (arrow) arrow.textContent = "";
            });
            headerCell.classList.add("td-sort-active");
            var activeArrow = headerCell.querySelector("[data-sort-arrow]");
            if (activeArrow) activeArrow.textContent = state.dir === "asc" ? "▲" : "▼";
        }

        headerCells.forEach(function (headerCell, columnIndex) {
            headerCell.addEventListener("click", function () {
                sortBy(headerCell, columnIndex);
            });
            headerCell.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    sortBy(headerCell, columnIndex);
                }
            });
        });
    });
});
