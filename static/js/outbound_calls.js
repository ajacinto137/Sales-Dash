// Rep Profile -- Outbound Call Performance (templates/_outbound_calls.html).
// Loads KPIs + the Outbound Call Volume series from
// <profile>/calls/summary, re-fetches only the series (/calls/volume) on
// a Group-by change (the date range decides which calls count; Group by
// only reshapes the X axis -- "auto" lets the server pick from the
// range, and the dropdown lists only groupings allowed for that range), and pages Outbound Call History (/calls/history)
// lazily the first time that section is opened. All three read the
// app's own database; HubSpot is never called from the browser. Chart
// styling reuses charts.js's shared helpers (getChartTokens,
// buildTooltipConfig, baseScaleOptions, baseAnimation, withAlpha).
document.addEventListener("DOMContentLoaded", function () {
    const root = document.getElementById("outbound-calls");
    if (!root || !root.querySelector("[data-calls-kpis]")) return;

    const base = root.dataset.callsBase;
    const canViewHistory = root.dataset.canViewHistory === "true";

    const rangeSelect = root.querySelector("[data-calls-range]");
    const customWrap = root.querySelector("[data-calls-custom]");
    const startInput = root.querySelector("[data-calls-start]");
    const endInput = root.querySelector("[data-calls-end]");
    const applyBtn = root.querySelector("[data-calls-apply]");
    const errorEl = root.querySelector("[data-calls-error]");
    const canvas = root.querySelector("[data-calls-chart]");
    const chartSkeleton = root.querySelector("[data-calls-chart-skeleton]");
    const emptyEl = root.querySelector("[data-calls-empty]");
    const scaleSelect = root.querySelector("[data-calls-scale]");
    const historyEl = root.querySelector("[data-calls-history]");
    const historyCount = root.querySelector("[data-history-count]");

    const state = {
        range: root.dataset.defaultRange,
        start: "",
        end: "",
        scale: "auto",
        chartType: "line",  // default by request, 2026-10-20 (was bar)
        series: [],
        history: { page: 1, outcome: "", order: "desc", loadedFor: null },
    };
    let chart = null;
    // Separate counters so a time-scale change mid-load supersedes only
    // the chart, never the KPIs.
    let summarySeq = 0;
    let chartSeq = 0;

    function rangeParams() {
        const params = new URLSearchParams({ range: state.range });
        if (state.range === "custom") {
            params.set("start", state.start);
            params.set("end", state.end);
        }
        return params;
    }

    function fetchJson(path, params) {
        return fetch(base + path + "?" + params.toString(), { headers: { Accept: "application/json" } })
            .then(function (res) {
                return res.json().catch(function () { return {}; }).then(function (data) {
                    if (!res.ok || !data.ok) {
                        throw new Error(data.error || "Could not load call data (HTTP " + res.status + ").");
                    }
                    return data;
                });
            });
    }

    function showError(message) {
        errorEl.textContent = message || "";
        errorEl.style.display = message ? "inline" : "none";
    }

    // ---------------- KPIs ----------------

    function setKpi(key, value, sub) {
        root.querySelector('[data-kpi="' + key + '"]').textContent = value;
        root.querySelector('[data-kpi-sub="' + key + '"]').textContent = sub || "";
    }

    function showKpiSkeletons() {
        root.querySelectorAll("[data-kpi], [data-pace]").forEach(function (el) {
            el.innerHTML = '<span class="td-skeleton td-skeleton--value"></span>';
        });
        root.querySelectorAll("[data-kpi-sub], [data-pace-sub]").forEach(function (el) { el.textContent = ""; });
    }

    function setPace(key, value, sub) {
        root.querySelector('[data-pace="' + key + '"]').textContent = value;
        root.querySelector('[data-pace-sub="' + key + '"]').textContent = sub || "";
    }

    function renderPace(k) {
        setPace(
            "calls_per_hour",
            k.calls_per_hour === null ? "N/A" : k.calls_per_hour.toFixed(1),
            k.working_days
                ? k.working_time_display + " calling across " + k.working_days.toLocaleString() +
                  " day" + (k.working_days === 1 ? "" : "s") + " (first to last call each day)"
                : ""
        );
        setPace("last_call", k.last_call_display || "No calls in range", "");
    }

    function renderKpis(k) {
        const fmt = new Intl.NumberFormat();
        setKpi("total_calls", fmt.format(k.total_calls));
        setKpi("connected_calls", fmt.format(k.connected_calls));
        setKpi("connection_rate", k.connection_rate === null ? "N/A" : k.connection_rate.toFixed(1) + "%");
        setKpi("total_talk", k.total_talk_display || "0m 0s");
        setKpi(
            "avg_duration",
            k.avg_duration_display || "N/A",
            k.calls_with_duration < k.total_calls
                ? "of " + fmt.format(k.calls_with_duration) + " calls with a logged duration"
                : ""
        );
        setKpi(
            "calls_per_sale",
            k.calls_per_sale === null ? "N/A" : k.calls_per_sale.toFixed(1),
            fmt.format(k.sales) + " outbound sale" + (k.sales === 1 ? "" : "s") + " in range"
        );
    }

    // ---------------- Chart ----------------

    function renderChart() {
        const series = state.series;
        const total = series.reduce(function (sum, b) { return sum + b.count; }, 0);
        chartSkeleton.style.display = "none";
        emptyEl.style.display = total === 0 ? "block" : "none";
        canvas.style.display = total === 0 ? "none" : "block";
        if (total === 0) {
            if (chart) { chart.destroy(); chart = null; }
            return;
        }

        const tokens = getChartTokens();
        const color = tokens.cyan;
        const labels = series.map(function (b) { return b.label; });
        const data = series.map(function (b) { return b.count; });
        const isLine = state.chartType === "line";
        const dataset = {
            data: data,
            backgroundColor: isLine ? withAlpha(color, 0.12) : color,
            borderColor: color,
            borderWidth: isLine ? 2 : 0,
            borderRadius: isLine ? 0 : tokens.barRadius,
            borderSkipped: false,
            maxBarThickness: 28,
            fill: isLine,
            tension: 0.25,
            pointRadius: isLine && series.length <= 60 ? 2.5 : 0,
            pointHoverRadius: 4,
            pointBackgroundColor: color,
        };

        // Same chart type -> update in place so Chart.js animates the
        // transition; a bar<->line switch needs a fresh chart.
        if (chart && chart.config.type === state.chartType) {
            chart.data.labels = labels;
            chart.data.datasets[0] = dataset;
            chart.update();
            return;
        }
        if (chart) chart.destroy();
        chart = new Chart(canvas, {
            type: state.chartType,
            data: { labels: labels, datasets: [dataset] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: baseAnimation(),
                interaction: { mode: "index", intersect: false },
                scales: {
                    x: {
                        ...baseScaleOptions(tokens),
                        grid: { display: false },
                        ticks: { ...baseScaleOptions(tokens).ticks, autoSkip: true, maxRotation: 0, maxTicksLimit: 12 },
                    },
                    y: { ...baseScaleOptions(tokens), beginAtZero: true, ticks: { ...baseScaleOptions(tokens).ticks, precision: 0 } },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: buildTooltipConfig(tokens, {
                        title: function (items) { return state.series[items[0].dataIndex].label; },
                        label: function (item) {
                            const n = state.series[item.dataIndex].count;
                            return n.toLocaleString() + " outbound call" + (n === 1 ? "" : "s");
                        },
                    }),
                },
            },
        });
    }

    function showChartLoading() {
        chartSkeleton.style.display = "block";
        canvas.style.display = "none";
        emptyEl.style.display = "none";
    }

    // ---------------- Group by ----------------

    function titleCase(word) {
        return word.charAt(0).toUpperCase() + word.slice(1);
    }

    // Rebuilds the Group-by options from the API: "Auto (<what auto
    // chose>)" plus only the groupings allowed for the current range.
    function renderScaleOptions(data) {
        scaleSelect.innerHTML = "";
        const auto = document.createElement("option");
        auto.value = "auto";
        auto.textContent = "Auto (" + titleCase(data.auto_scale) + ")";
        scaleSelect.appendChild(auto);
        data.allowed_scales.forEach(function (scale) {
            const opt = document.createElement("option");
            opt.value = scale;
            opt.textContent = titleCase(scale);
            scaleSelect.appendChild(opt);
        });
        scaleSelect.value = data.scale_mode === "auto" ? "auto" : data.scale;
        scaleSelect.disabled = data.allowed_scales.length <= 1;
    }

    // ---------------- Loading ----------------

    function loadSummary() {
        const seq = ++summarySeq;
        const chartAtStart = ++chartSeq;
        showError("");
        showKpiSkeletons();
        showChartLoading();
        const params = rangeParams();
        params.set("scale", state.scale);
        fetchJson("/summary", params)
            .then(function (data) {
                if (seq !== summarySeq) return;
                renderKpis(data.kpis);
                renderPace(data.kpis);
                if (chartAtStart !== chartSeq) return;
                renderScaleOptions(data);
                state.series = data.series;
                renderChart();
            })
            .catch(function (err) {
                if (seq !== summarySeq) return;
                // Never fall back to zeros on failure -- an outage must
                // not look like "no calls".
                root.querySelectorAll("[data-kpi], [data-pace]").forEach(function (el) { el.textContent = "—"; });
                chartSkeleton.style.display = "none";
                showError(err.message);
            });
        state.history.page = 1;
        state.history.loadedFor = null;
        if (historyEl.open) loadHistory();
        else historyCount.textContent = "";
    }

    function loadVolume() {
        const seq = ++chartSeq;
        showError("");
        showChartLoading();
        const params = rangeParams();
        params.set("scale", state.scale);
        fetchJson("/volume", params)
            .then(function (data) {
                if (seq !== chartSeq) return;
                renderScaleOptions(data);
                state.series = data.series;
                renderChart();
            })
            .catch(function (err) {
                if (seq !== chartSeq) return;
                chartSkeleton.style.display = "none";
                showError(err.message);
            });
    }

    // ---------------- History ----------------

    const rowsEl = root.querySelector("[data-history-rows]");
    const outcomeSelect = root.querySelector("[data-history-outcome]");
    const prevBtn = root.querySelector("[data-history-prev]");
    const nextBtn = root.querySelector("[data-history-next]");
    const pageLabel = root.querySelector("[data-history-page]");
    const sortBtn = root.querySelector("[data-history-sort]");
    const sortArrow = root.querySelector("[data-history-sort-arrow]");
    let historySeq = 0;

    function cell(text, className) {
        const td = document.createElement("td");
        td.textContent = text === null || text === undefined || text === "" ? "—" : text;
        if (className) td.className = className;
        return td;
    }

    // Opens in a new tab; clicking it must not also toggle the row's
    // details. URLs are already restricted to https:// server-side.
    function linkCell(url, text, title) {
        if (!url) return cell(text);
        const td = document.createElement("td");
        const a = document.createElement("a");
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.className = "td-calls-link";
        a.textContent = text || "Open";
        a.title = title;
        a.addEventListener("click", function (e) { e.stopPropagation(); });
        a.addEventListener("keydown", function (e) { e.stopPropagation(); });
        td.appendChild(a);
        return td;
    }

    function renderHistory(data) {
        rowsEl.innerHTML = "";
        if (!data.calls.length) {
            const tr = document.createElement("tr");
            const td = cell("No outbound calls match this range and filter.", "td-empty-row");
            td.colSpan = 8;
            tr.appendChild(td);
            rowsEl.appendChild(tr);
        }
        data.calls.forEach(function (c) {
            const tr = document.createElement("tr");
            tr.className = "td-calls-row";
            tr.tabIndex = 0;
            tr.setAttribute("aria-expanded", "false");
            tr.appendChild(cell(c.date));
            tr.appendChild(cell(c.time));
            tr.appendChild(cell(c.duration));
            const outcome = cell(c.outcome || "No outcome logged");
            if (c.connected) outcome.classList.add("td-green");
            tr.appendChild(outcome);
            tr.appendChild(cell(c.status));
            tr.appendChild(cell(c.to_number));
            tr.appendChild(linkCell(c.contact_url, c.contact_name, "Open " + (c.contact_name || "contact") + " in HubSpot"));
            tr.appendChild(linkCell(c.recording_url, c.recording_url ? "▶ Listen" : null, "Play recording in HubSpot (opens a new tab)"));

            const detail = document.createElement("tr");
            detail.className = "td-calls-detail";
            detail.style.display = "none";
            const td = document.createElement("td");
            td.colSpan = 8;
            [["Title", c.title], ["Called from", c.from_number], ["HubSpot call ID", c.id]].forEach(function (pair) {
                const span = document.createElement("span");
                span.className = "td-calls-detail-item";
                const label = document.createElement("span");
                label.className = "td-calls-detail-label";
                label.textContent = pair[0];
                span.appendChild(label);
                span.appendChild(document.createTextNode(pair[1] || "—"));
                td.appendChild(span);
            });
            detail.appendChild(td);

            function toggle() {
                const open = detail.style.display === "none";
                detail.style.display = open ? "table-row" : "none";
                tr.setAttribute("aria-expanded", open ? "true" : "false");
            }
            tr.addEventListener("click", toggle);
            tr.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
            });
            rowsEl.appendChild(tr);
            rowsEl.appendChild(detail);
        });

        const current = outcomeSelect.value;
        outcomeSelect.innerHTML = "";
        const all = document.createElement("option");
        all.value = "";
        all.textContent = "All outcomes";
        outcomeSelect.appendChild(all);
        data.outcomes.forEach(function (o) {
            const opt = document.createElement("option");
            opt.value = o.value;
            opt.textContent = o.label + " (" + o.count.toLocaleString() + ")";
            outcomeSelect.appendChild(opt);
        });
        outcomeSelect.value = data.outcomes.some(function (o) { return o.value === current; }) ? current : "";

        pageLabel.textContent = "Page " + data.page + " of " + data.pages;
        prevBtn.disabled = data.page <= 1;
        nextBtn.disabled = data.page >= data.pages;
        historyCount.textContent = data.total.toLocaleString() + " call" + (data.total === 1 ? "" : "s");
        sortArrow.innerHTML = state.history.order === "desc" ? "&#9660;" : "&#9650;";
    }

    function loadHistory() {
        if (!canViewHistory) return;
        const seq = ++historySeq;
        const params = rangeParams();
        params.set("page", state.history.page);
        params.set("order", state.history.order);
        if (state.history.outcome) params.set("outcome", state.history.outcome);
        rowsEl.innerHTML = '<tr><td colspan="8" class="td-empty-row">Loading…</td></tr>';
        fetchJson("/history", params)
            .then(function (data) {
                if (seq !== historySeq) return;
                state.history.loadedFor = params.toString();
                renderHistory(data);
            })
            .catch(function (err) {
                if (seq !== historySeq) return;
                rowsEl.innerHTML = "";
                const tr = document.createElement("tr");
                const td = cell(err.message, "td-empty-row");
                td.colSpan = 8;
                tr.appendChild(td);
                rowsEl.appendChild(tr);
            });
    }

    if (canViewHistory) {
        historyEl.addEventListener("toggle", function () {
            if (historyEl.open && state.history.loadedFor === null) loadHistory();
        });
        outcomeSelect.addEventListener("change", function () {
            state.history.outcome = outcomeSelect.value;
            state.history.page = 1;
            loadHistory();
        });
        prevBtn.addEventListener("click", function () { state.history.page -= 1; loadHistory(); });
        nextBtn.addEventListener("click", function () { state.history.page += 1; loadHistory(); });
        sortBtn.addEventListener("click", function () {
            state.history.order = state.history.order === "desc" ? "asc" : "desc";
            state.history.page = 1;
            loadHistory();
        });
    }

    // ---------------- Controls ----------------

    rangeSelect.addEventListener("change", function () {
        if (rangeSelect.value === "custom") {
            customWrap.style.display = "flex";
            return;
        }
        customWrap.style.display = "none";
        state.range = rangeSelect.value;
        state.scale = "auto";
        loadSummary();
    });

    applyBtn.addEventListener("click", function () {
        if (!startInput.value || !endInput.value) {
            showError("Pick both a start and an end date.");
            return;
        }
        state.range = "custom";
        state.start = startInput.value;
        state.end = endInput.value;
        state.scale = "auto";
        loadSummary();
    });

    scaleSelect.addEventListener("change", function () {
        state.scale = scaleSelect.value;
        loadVolume();
    });

    root.querySelectorAll("[data-calls-chart-type] [data-chart-type]").forEach(function (btn) {
        btn.addEventListener("click", function () {
            if (btn.dataset.chartType === state.chartType) return;
            root.querySelectorAll("[data-calls-chart-type] [data-chart-type]").forEach(function (b) {
                b.classList.toggle("td-chart-toggle-active", b === btn);
            });
            state.chartType = btn.dataset.chartType;
            renderChart();
        });
    });

    loadSummary();
});
