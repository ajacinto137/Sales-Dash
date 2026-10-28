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

    // Team Leaderboard overview KPIs: Team / Door Knockers / Total toggle
    // (2026-10-23). Remembers the last choice in this browser.
    document.querySelectorAll("[data-kpi-switch]").forEach(function (root) {
        var buttons = root.querySelectorAll("[data-kpi-view]");
        if (!buttons.length) return;
        function show(view) {
            if (!root.querySelector('[data-kpi-panel="' + view + '"]')) view = "team";
            buttons.forEach(function (b) {
                var on = b.getAttribute("data-kpi-view") === view;
                b.classList.toggle("td-chart-toggle-active", on);
                b.setAttribute("aria-pressed", on ? "true" : "false");
            });
            root.querySelectorAll("[data-kpi-panel]").forEach(function (p) { p.hidden = p.getAttribute("data-kpi-panel") !== view; });
            root.querySelectorAll("[data-kpi-hint]").forEach(function (h) { h.hidden = h.getAttribute("data-kpi-hint") !== view; });
            // Other sections that follow this toggle (Sales Calendar &
            // Monthly Sales Trend) -- charts.js draws them on first show.
            document.querySelectorAll("[data-kpi-follow]").forEach(function (p) { p.hidden = p.getAttribute("data-kpi-follow") !== view; });
            document.dispatchEvent(new CustomEvent("td:kpi-view", { detail: { view: view } }));
            try { localStorage.setItem("td-kpi-view", view); } catch (e) { /* storage unavailable */ }
        }
        buttons.forEach(function (b) {
            b.addEventListener("click", function () { show(b.getAttribute("data-kpi-view")); });
        });
        var saved = null;
        try { saved = localStorage.getItem("td-kpi-view"); } catch (e) { /* storage unavailable */ }
        if (saved) show(saved);
    });

    // Click-to-sort for plain tables marked <table data-sortable> (added
    // 2026-10-22, Door Knockers pages). Each header sorts its column --
    // first click descending for numbers, ascending for text; again to
    // flip. Values come from a cell's data-sort-value, else its text:
    // "1,316", "71.3%" and "43.2" sort as numbers; "—"/"n/a"/blank go
    // last either way. Rows with data-sort-fixed (Total/Combined) stay
    // at the bottom; headers with data-no-sort are skipped.
    function sortValue(cell) {
        if (!cell) return null;
        var raw = cell.hasAttribute("data-sort-value") ? cell.getAttribute("data-sort-value") : cell.textContent;
        raw = raw.trim();
        if (!raw || raw === "—" || raw.toLowerCase() === "n/a") return null;
        var numeric = raw.replace(/[,%$]/g, "");
        if (/^-?\d+(\.\d+)?$/.test(numeric)) return parseFloat(numeric);
        return raw.toLowerCase();
    }

    document.querySelectorAll("table[data-sortable]").forEach(function (table) {
        var headers = table.querySelectorAll("thead th");
        var tbody = table.tBodies[0];
        if (!tbody) return;
        var state = { index: null, dir: null };

        headers.forEach(function (th, index) {
            if (th.hasAttribute("data-no-sort")) return;
            th.classList.add("td-th-sortable");
            th.setAttribute("tabindex", "0");
            th.setAttribute("aria-sort", "none");
            var arrow = document.createElement("span");
            arrow.className = "td-sort-arrow";
            th.appendChild(arrow);

            function sortBy() {
                var rows = Array.prototype.slice.call(tbody.rows);
                var fixed = rows.filter(function (r) { return r.hasAttribute("data-sort-fixed"); });
                var movable = rows.filter(function (r) { return !r.hasAttribute("data-sort-fixed"); });
                var sample = movable.map(function (r) { return sortValue(r.cells[index]); }).filter(function (v) { return v !== null; })[0];
                var numeric = typeof sample === "number";
                var dir = state.index === index ? (state.dir === "asc" ? "desc" : "asc") : (numeric ? "desc" : "asc");
                state = { index: index, dir: dir };

                movable.sort(function (a, b) {
                    var va = sortValue(a.cells[index]);
                    var vb = sortValue(b.cells[index]);
                    if (va === null && vb === null) return 0;
                    if (va === null) return 1;
                    if (vb === null) return -1;
                    var cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
                    return dir === "asc" ? cmp : -cmp;
                });
                movable.concat(fixed).forEach(function (r) { tbody.appendChild(r); });

                headers.forEach(function (h) {
                    var a = h.querySelector(".td-sort-arrow");
                    if (a) a.textContent = "";
                    if (!h.hasAttribute("data-no-sort")) h.setAttribute("aria-sort", "none");
                });
                arrow.textContent = dir === "asc" ? "▲" : "▼";
                th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
            }

            th.addEventListener("click", sortBy);
            th.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sortBy(); }
            });
        });
    });

    // Blitzes: "Combine selected" (added 2026-10-22). A row can stand for
    // several blitzes (Group by State) -- its extra checkboxes are hidden
    // and follow the visible one. The button needs 2+ blitzes selected.
    document.querySelectorAll("[data-blitz-group-form]").forEach(function (form) {
        var button = form.querySelector("[data-blitz-combine]");
        function refresh() {
            var picked = {};
            form.querySelectorAll("[data-blitz-check]:checked").forEach(function (box) { picked[box.value] = true; });
            var count = Object.keys(picked).length;
            button.disabled = count < 2;
            button.textContent = count >= 2 ? "Combine " + count + " blitzes" : "Combine selected";
        }
        form.querySelectorAll("[data-blitz-check]").forEach(function (box) {
            box.addEventListener("change", function () {
                box.parentElement.querySelectorAll("[data-blitz-check]").forEach(function (sibling) {
                    sibling.checked = box.checked;
                });
                refresh();
            });
        });
        refresh();
    });

    // Bulk Account View cards open that customer's Customer Profile
    // (added 2026-10-22). Clicks on anything interactive inside the card
    // (rep link, Vision link, Needs Attention controls) keep their own
    // behavior; the name itself is a real link for cmd/ctrl-click.
    document.querySelectorAll(".td-bulk-card-link").forEach(function (card) {
        card.addEventListener("click", function (e) {
            if (e.target.closest("a, button, input, select, textarea, label, details, [data-attention-block]")) {
                return;
            }
            if (window.getSelection && String(window.getSelection())) {
                return;  // let people select/copy text without navigating
            }
            var href = card.getAttribute("data-href");
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
        var activateTab = function (target) {
            var matched = false;
            tabButtons.forEach(function (b) {
                var isMatch = b.getAttribute("data-tab") === target;
                if (isMatch) matched = true;
                b.classList.toggle("td-tab-active", isMatch);
            });
            if (!matched) return false;
            tabPanels.forEach(function (panel) {
                panel.style.display = panel.getAttribute("data-tab-panel") === target ? "" : "none";
            });
            return true;
        };

        tabButtons.forEach(function (button) {
            button.addEventListener("click", function () {
                activateTab(button.getAttribute("data-tab"));
            });
        });

        // A link elsewhere (e.g. the Individual Rep Leaderboard's Needs
        // Attention metric cell) can open straight to a specific tab via
        // a #<data-tab-value> URL fragment, e.g. .../reps/<rep>#needs-attention
        // -- honor it on load instead of always defaulting to Overview.
        var hashTarget = window.location.hash.replace(/^#/, "");
        if (hashTarget) {
            activateTab(hashTarget);
        }
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

    // Sales by Town's Internal / Door Knockers / Combined dropdown
    // (2026-10-28): shows that source's block (each has its own state toggle).
    document.querySelectorAll("[data-town-source-select]").forEach(function (select) {
        var section = select.closest("section");
        select.addEventListener("change", function () {
            section.querySelectorAll("[data-town-source-panel]").forEach(function (panel) {
                panel.style.display = panel.getAttribute("data-town-source-panel") === select.value ? "" : "none";
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

    // Formula hover box (added 2026-10-23): hovering anything with a
    // data-formula shows its equation. A table cell uses its column
    // header's formula, so headers carry it once per column.
    var formulaTip = null;
    function formulaFor(el) {
        var own = el.closest("[data-formula]");
        if (own) return own;
        var cell = el.closest("td");
        var table = cell && cell.closest("table");
        if (!table || !table.tHead) return null;
        var headerRow = table.tHead.rows[table.tHead.rows.length - 1];
        var th = headerRow && headerRow.cells[cell.cellIndex];
        return th && th.hasAttribute("data-formula") ? th : null;
    }
    function hideFormula() {
        if (formulaTip) formulaTip.hidden = true;
    }
    document.addEventListener("mouseover", function (e) {
        var source = formulaFor(e.target);
        var text = source && source.getAttribute("data-formula").trim();
        if (!text) {
            hideFormula();
            return;
        }
        if (!formulaTip) {
            formulaTip = document.createElement("div");
            formulaTip.className = "td-formula-tip";
            formulaTip.setAttribute("role", "tooltip");
            document.body.appendChild(formulaTip);
        }
        formulaTip.textContent = text;
        formulaTip.hidden = false;
        var anchor = e.target.closest("td, th, [data-formula]") || source;
        var box = anchor.getBoundingClientRect();
        var tip = formulaTip.getBoundingClientRect();
        var left = Math.min(Math.max(8, box.left + box.width / 2 - tip.width / 2), window.innerWidth - tip.width - 8);
        var top = box.top - tip.height - 8;
        if (top < 8) top = box.bottom + 8;
        formulaTip.style.left = left + "px";
        formulaTip.style.top = top + "px";
    });
    window.addEventListener("scroll", hideFormula, true);

    // A <select> or checkbox with data-autosubmit submits its form on
    // change (Knocker Leaderboard's Card on File view and Active only
    // filter, 2026-10-26).
    document.querySelectorAll("select[data-autosubmit], input[data-autosubmit]").forEach(function (field) {
        field.addEventListener("change", function () { field.form.submit(); });
    });
});
