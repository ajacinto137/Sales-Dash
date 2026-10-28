// Chart factories shared by the Individual Sales Profile (MonthlySalesChart,
// SalesByChannelChart, HourlySalesChart) and the Team Leaderboard dashboard
// (MonthlySalesTrendChart, plus team-wide instances of SalesByChannelChart
// and HourlySalesChart via the same factories below). Plain vanilla JS, no
// bundler -- same convention as team_dashboard.js. Chart.js v4 is loaded via
// CDN in each page's <head> before this file. Each factory reads its data
// from data-* attributes on its <canvas> (rendered server-side via |tojson)
// rather than a separate JSON endpoint.

function getChartTokens() {
    const styles = getComputedStyle(document.documentElement);
    const read = (name) => styles.getPropertyValue(name).trim();
    return {
        gridColor: read("--td-chart-grid"),
        axisText: read("--td-chart-axis-text"),
        tooltipBg: read("--td-chart-tooltip-bg"),
        tooltipBorder: read("--td-chart-tooltip-border"),
        barRadius: parseFloat(read("--td-chart-bar-radius")) || 4,
        lineWidth: parseFloat(read("--td-chart-line-width")) || 1.5,
        lineCumulative: read("--td-chart-line-cumulative"),
        teamAverage: read("--td-chart-team-average"),
        accentBlue: read("--td-accent-blue"),
        accentBlueBg: read("--td-accent-blue-bg"),
        accentLime: read("--td-accent-lime"),
        accentMagenta: read("--td-accent-magenta"),
        accentMagentaBg: read("--td-accent-magenta-bg"),
        green: read("--td-green"),
        greenBg: read("--td-green-bg"),
        yellow: read("--td-yellow"),
        yellowBg: read("--td-yellow-bg"),
        orange: read("--td-orange"),
        orangeBg: read("--td-orange-bg"),
        purple: read("--td-purple"),
        purpleBg: read("--td-purple-bg"),
        cyan: read("--td-cyan"),
        cyanBg: read("--td-cyan-bg"),
        panel: read("--td-panel"),
        statusColors: {
            installed: read("--td-chart-status-installed"),
            pending: read("--td-chart-status-pending"),
            needs_attention: read("--td-chart-status-needs-attention"),
            cancelled: read("--td-chart-status-cancelled"),
            service_cancelled: read("--td-chart-service-cancel"),
        },
        serviceCancel: read("--td-chart-service-cancel"),
        channelPalette: [
            read("--td-chart-channel-1"),
            read("--td-chart-channel-2"),
            read("--td-chart-channel-3"),
            read("--td-chart-channel-4"),
            read("--td-chart-channel-5"),
            read("--td-chart-channel-6"),
            read("--td-chart-channel-7"),
            read("--td-chart-channel-8"),
            read("--td-chart-channel-9"),
        ],
    };
}

function buildTooltipConfig(tokens, callbacks) {
    return {
        backgroundColor: tokens.tooltipBg,
        borderColor: tokens.tooltipBorder,
        borderWidth: 1,
        padding: 10,
        cornerRadius: 6,
        titleColor: "#f4f4f5",
        bodyColor: "#9a9aa2",
        titleFont: { size: 12, weight: "700" },
        bodyFont: { size: 11.5 },
        displayColors: false,
        callbacks,
    };
}

function baseScaleOptions(tokens) {
    return {
        grid: { color: tokens.gridColor, drawTicks: false },
        ticks: { color: tokens.axisText, font: { size: 11 } },
        border: { display: false },
    };
}

function baseAnimation() {
    return { duration: 400, easing: "easeOutQuart" };
}

function createMonthlySalesChart(canvasEl, tokens) {
    const days = JSON.parse(canvasEl.dataset.days || "[]");
    const year = canvasEl.dataset.year;
    const month = canvasEl.dataset.month;
    const drilldownBase = canvasEl.dataset.drilldownBase;

    let running = 0;
    const cumulative = days.map((d) => (running += d.count));

    const chart = new Chart(canvasEl, {
        type: "bar",
        data: {
            labels: days.map((d) => d.day),
            datasets: [
                {
                    label: "Sales",
                    data: days.map((d) => d.count),
                    backgroundColor: tokens.accentBlue,
                    borderRadius: tokens.barRadius,
                    borderSkipped: false,
                    maxBarThickness: 18,
                    order: 2,
                },
                {
                    label: "Cumulative",
                    data: cumulative,
                    type: "line",
                    borderColor: tokens.lineCumulative,
                    borderWidth: tokens.lineWidth,
                    borderDash: [4, 3],
                    pointRadius: 0,
                    tension: 0.25,
                    yAxisID: "y1",
                    order: 1,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            onClick: (_evt, elements) => {
                if (!elements.length || !drilldownBase) return;
                const day = days[elements[0].index].day;
                window.location.href = `${drilldownBase}&year=${year}&month=${month}&day=${day}`;
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length && drilldownBase ? "pointer" : "default";
            },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
                y1: { display: false, beginAtZero: true },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => {
                        const day = days[items[0].dataIndex];
                        return `${monthLabelForDay(year, month, day.day)}`;
                    },
                    label: (item) => {
                        if (item.dataset.label === "Cumulative") return null;
                        const day = days[item.dataIndex];
                        return `${day.count} Sale${day.count === 1 ? "" : "s"}`;
                    },
                    afterBody: (items) => {
                        const day = days[items[0].dataIndex];
                        if (!day.channels || !day.channels.length) return [];
                        return ["", ...day.channels.map((c) => `${c.channel}: ${c.count}`)];
                    },
                }),
            },
        },
    });
    return chart;
}

function monthLabelForDay(year, month, day) {
    const d = new Date(Number(year), Number(month) - 1, Number(day));
    return d.toLocaleDateString("en-US", { month: "long", day: "numeric" });
}

function createMonthlySalesTrendChart(canvasEl, tokens) {
    const months = JSON.parse(canvasEl.dataset.months || "[]");

    const chart = new Chart(canvasEl, {
        type: "line",
        data: {
            labels: months.map((m) => m.month_label),
            datasets: [
                {
                    label: "Sales",
                    data: months.map((m) => m.count),
                    borderColor: tokens.accentBlue,
                    borderWidth: tokens.lineWidth + 1,
                    backgroundColor: tokens.accentBlueBg,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    pointHoverBackgroundColor: tokens.accentBlue,
                    pointHoverBorderWidth: 0,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => months[items[0].dataIndex].month_label,
                    label: (item) => {
                        const count = months[item.dataIndex].count;
                        return `${count} Sale${count === 1 ? "" : "s"}`;
                    },
                }),
            },
        },
    });
    return chart;
}

// Month to Date mode of the Monthly Sales card (added 2026-10-15) --
// cumulative sales by day of month: current month to date (stops at
// today), the full previous month, and the YTD average month (mean
// cumulative curve of this year's completed months). Data comes from
// calculate_month_to_date_comparison(); null entries are gaps.
// ---------------- Door Knocker blitzes (added 2026-10-22) ----------------

const BLITZ_STATUSES = [
    ["installed", "Installed"],
    ["pending", "Pending"],
    ["needs_attention", "Needs Attention"],
    ["cancelled", "Cancelled"],
    ["service_cancelled", "Service Cancelled"],
];

// Monthly Sales by Status merges order + service cancels into one segment.
const MONTHLY_STATUSES = [
    ["installed", "Installed"],
    ["pending", "Pending"],
    ["needs_attention", "Needs Attention"],
    ["cancels", "Cancels"],
];

// Sales per calendar day across one blitz. Click a bar -> that day's accounts.
function createBlitzDailyChart(canvasEl, tokens) {
    const days = JSON.parse(canvasEl.dataset.days || "[]");
    const base = canvasEl.dataset.drilldownBase;
    return new Chart(canvasEl, {
        type: "bar",
        data: {
            labels: days.map((d) => d.label),
            datasets: [{
                label: "Sales",
                data: days.map((d) => d.count),
                backgroundColor: tokens.accentBlue,
                borderRadius: tokens.barRadius,
                borderSkipped: false,
                maxBarThickness: 22,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            onClick: (_evt, elements) => {
                if (!elements.length || !base) return;
                const [y, m, d] = days[elements[0].index].date.split("-").map(Number);
                window.location.href = `${base}&year=${y}&month=${m}&day=${d}`;
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length && base ? "pointer" : "default";
            },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false }, ticks: { ...baseScaleOptions(tokens).ticks, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true, ticks: { ...baseScaleOptions(tokens).ticks, precision: 0 } },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => days[items[0].dataIndex].label,
                    label: (item) => `${item.raw} sale${item.raw === 1 ? "" : "s"}`,
                }),
            },
        },
    });
}

// Account status per blitz as horizontal stacked bars (one row per blitz,
// or one row for a single blitz). Click a segment -> those accounts.
// Same stacked statuses as upright monthly columns (Door Knockers
// "Monthly Sales by Status", added 2026-10-27): one bar per sale month,
// MONTHLY_STATUSES stack to that month's sales, total printed on top.
function createMonthlyStatusChart(canvasEl, tokens) {
    return createStatusStackedChart(canvasEl, tokens, true);
}

// Monthly Sales by Status's "Internal vs Door Knockers" tab (added
// 2026-10-27): per month an Internal stack and a Door Knockers stack side
// by side (Chart.js `stack` groups), never summed. Knocker segments are
// the same status colors, faded, so each status reads the same in both.
// Rows: [{label, internal: {sales, installed, ..., url}, knockers: {...}}].
const COMPARE_GROUPS = [["internal", "Internal", 1], ["knockers", "Door Knockers", 0.5]];

function createStatusCompareChart(canvasEl, tokens) {
    const rows = JSON.parse(canvasEl.dataset.rows || "[]");
    const datasets = [];
    COMPARE_GROUPS.forEach(([group, groupLabel, alpha]) => {
        MONTHLY_STATUSES.forEach(([key, label]) => {
            datasets.push({
                label: `${groupLabel} · ${label}`,
                key,
                group,
                stack: group,
                data: rows.map((r) => r[group][key]),
                backgroundColor: withAlpha(tokens.statusColors[key === "cancels" ? "cancelled" : key], alpha),
                borderColor: tokens.panel,
                borderWidth: { top: 2 },
                borderSkipped: false,
                borderRadius: 0,
                maxBarThickness: 30,
            });
        });
    });
    const totalColor = getComputedStyle(canvasEl).getPropertyValue("--td-text-primary").trim() || tokens.axisText;
    const totals = {
        id: "compareTotals",
        afterDatasetsDraw(chart) {
            const { ctx } = chart;
            ctx.save();
            ctx.textAlign = "center";
            COMPARE_GROUPS.forEach(([group], g) => {
                const meta = chart.getDatasetMeta((g + 1) * MONTHLY_STATUSES.length - 1);
                if (meta.hidden) return;
                meta.data.forEach((bar, i) => {
                    const n = rows[i][group].sales;
                    if (!n) return;
                    const top = chart.scales.y.getPixelForValue(n);
                    ctx.font = "700 10.5px sans-serif";
                    ctx.fillStyle = totalColor;
                    ctx.fillText(n.toLocaleString(), bar.x, top - 14);
                    ctx.font = "600 9px sans-serif";
                    ctx.fillStyle = tokens.axisText;
                    ctx.fillText(group === "internal" ? "INT" : "DK", bar.x, top - 4);
                });
            });
            ctx.restore();
        },
    };
    return new Chart(canvasEl, {
        type: "bar",
        plugins: [totals],
        data: { labels: rows.map((r) => r.label), datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "nearest", axis: "x", intersect: true },
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const ds = datasets[elements[0].datasetIndex];
                const url = rows[elements[0].index][ds.group].url;
                if (url) window.location.href = `${url}&status=${ds.key}`;
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "default";
            },
            scales: {
                x: { ...baseScaleOptions(tokens), stacked: true, grid: { display: false } },
                y: { ...baseScaleOptions(tokens), stacked: true, beginAtZero: true, grace: "12%", ticks: { ...baseScaleOptions(tokens).ticks, precision: 0 } },
            },
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: { color: tokens.axisText, font: { size: 11, weight: "600" }, boxWidth: 10, boxHeight: 10, padding: 12 },
                },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => `${rows[items[0].dataIndex].label} · ${datasets[items[0].datasetIndex].group === "internal" ? "Internal" : "Door Knockers"}`,
                    label: (item) => {
                        const ds = datasets[item.datasetIndex];
                        const total = rows[item.dataIndex][ds.group].sales;
                        const pct = total ? ` (${((item.raw / total) * 100).toFixed(1)}%)` : "";
                        return `${MONTHLY_STATUSES.find(([k]) => k === ds.key)[1]}: ${item.raw}${pct}`;
                    },
                    footer: (items) => `Sales: ${rows[items[0].dataIndex][datasets[items[0].datasetIndex].group].sales.toLocaleString()}`,
                }),
            },
        },
    });
}

// Prints each stack's total (row.sales) above its column.
function stackTotalsPlugin(rows, color) {
    return {
        id: "stackTotals",
        afterDatasetsDraw(chart) {
            const meta = chart.getDatasetMeta(chart.data.datasets.length - 1);
            const { ctx } = chart;
            ctx.save();
            ctx.font = "700 11px sans-serif";
            ctx.textAlign = "center";
            ctx.fillStyle = color;
            meta.data.forEach((bar, i) => {
                if (!rows[i] || !rows[i].sales) return;
                ctx.fillText(rows[i].sales.toLocaleString(), bar.x, chart.scales.y.getPixelForValue(rows[i].sales) - 5);
            });
            ctx.restore();
        },
    };
}

function createStatusStackedChart(canvasEl, tokens, vertical) {
    const rows = JSON.parse(canvasEl.dataset.rows || "[]");
    const statuses = vertical ? MONTHLY_STATUSES : BLITZ_STATUSES;
    const countAxis = { ...baseScaleOptions(tokens), stacked: true, beginAtZero: true, ticks: { ...baseScaleOptions(tokens).ticks, precision: 0 } };
    const labelAxis = { ...baseScaleOptions(tokens), stacked: true, grid: { display: false } };
    if (vertical) countAxis.grace = "8%";
    return new Chart(canvasEl, {
        plugins: vertical ? [stackTotalsPlugin(rows, getComputedStyle(canvasEl).getPropertyValue("--td-text-primary").trim() || tokens.axisText)] : [],
        type: "bar",
        data: {
            labels: rows.map((r) => r.name),
            datasets: statuses.map(([key, label]) => ({
                label,
                key,
                data: rows.map((r) => r[key]),
                backgroundColor: tokens.statusColors[key === "cancels" ? "cancelled" : key],
                borderColor: tokens.panel,
                borderWidth: vertical ? { top: 2 } : { right: 2 },
                borderSkipped: false,
                borderRadius: 0,
                maxBarThickness: vertical ? 44 : 26,
            })),
        },
        options: {
            indexAxis: vertical ? "x" : "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "nearest", axis: vertical ? "x" : "y", intersect: true },
            onClick: (_evt, elements) => {
                if (!elements.length) return;
                const row = rows[elements[0].index];
                const key = statuses[elements[0].datasetIndex][0];
                if (row.url) window.location.href = `${row.url}&status=${key}`;
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "default";
            },
            scales: vertical ? { x: labelAxis, y: countAxis } : { x: countAxis, y: labelAxis },
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: { color: tokens.axisText, font: { size: 11, weight: "600" }, boxWidth: 10, boxHeight: 10, padding: 14 },
                },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => rows[items[0].dataIndex].name,
                    label: (item) => {
                        const total = rows[item.dataIndex].sales;
                        const pct = total ? ` (${((item.raw / total) * 100).toFixed(1)}%)` : "";
                        return `${item.dataset.label}: ${item.raw}${pct}`;
                    },
                    footer: (items) => `Sales: ${rows[items[0].dataIndex].sales.toLocaleString()}`,
                }),
            },
        },
    });
}

// Door Knockers team rates by sale month (added 2026-10-22): order cancel,
// needs attention and service cancel %, one % axis. Months still
// "maturing" (too recent for installs/cancels to have happened) draw
// dashed. Rows come from knockers.monthly_rates().
function createRateTrendChart(canvasEl, tokens) {
    const rows = JSON.parse(canvasEl.dataset.rows || "[]");
    const series = [
        ["order_cancel_rate", "Order cancel rate", tokens.statusColors.cancelled, "cancelled", "sales"],
        ["needs_attention_rate", "Needs attention rate", tokens.statusColors.needs_attention, "needs_attention", "sales"],
        ["service_cancel_rate", "Service cancel rate", tokens.serviceCancel, "service_cancelled", "ever_installed"],
    ];
    const dashedIfMaturing = (ctx) => (rows[ctx.p1DataIndex] && rows[ctx.p1DataIndex].maturing ? [5, 4] : undefined);
    return new Chart(canvasEl, {
        type: "line",
        data: {
            labels: rows.map((r) => r.label),
            datasets: series.map(([key, label, color, countKey, baseKey]) => ({
                label,
                key,
                countKey,
                baseKey,
                data: rows.map((r) => (r[key] === null ? null : Math.round(r[key] * 10) / 10)),
                borderColor: color,
                backgroundColor: color,
                borderWidth: 2,
                tension: 0.25,
                pointRadius: 4,
                pointHoverRadius: 6,
                pointBorderColor: tokens.panel,
                pointBorderWidth: 2,
                segment: { borderDash: dashedIfMaturing },
                spanGaps: false,
            })),
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: {
                    ...baseScaleOptions(tokens),
                    beginAtZero: true,
                    ticks: { ...baseScaleOptions(tokens).ticks, callback: (v) => `${v}%` },
                },
            },
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: { color: tokens.axisText, font: { size: 11, weight: "600" }, boxWidth: 10, boxHeight: 2, padding: 14 },
                },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => {
                        const row = rows[items[0].dataIndex];
                        return `${row.label} sales${row.maturing ? " (still maturing)" : ""}`;
                    },
                    label: (item) => {
                        if (item.raw === null || item.raw === undefined) return null;
                        const row = rows[item.dataIndex];
                        const ds = item.dataset;
                        return `${ds.label}: ${item.raw.toFixed(1)}% (${row[ds.countKey]} of ${row[ds.baseKey]})`;
                    },
                }),
            },
        },
    });
}

// "Week to Date" mode of the Monthly Sales card (added 2026-10-22, Door
// Knockers dashboard): cumulative sales Mon-Sun this week vs the average
// of the last N completed weeks -- see calculate_week_to_date_comparison().
function createWeekToDateChart(canvasEl, tokens) {
    const wtd = JSON.parse(canvasEl.dataset.wtd || "{}");
    const line = (extra) => ({
        fill: false, tension: 0.25, pointRadius: 0, pointHoverRadius: 4, pointHoverBorderWidth: 0, ...extra,
    });
    return new Chart(canvasEl, {
        type: "line",
        data: {
            labels: wtd.days,
            datasets: [
                line({
                    label: "This week", key: "current", data: wtd.current,
                    borderColor: tokens.accentBlue, borderWidth: tokens.lineWidth + 1,
                    backgroundColor: tokens.accentBlueBg, fill: true,
                    pointRadius: 3, pointBackgroundColor: tokens.accentBlue,
                    pointHoverBackgroundColor: tokens.accentBlue, order: 1,
                }),
                line({
                    label: `Average week (last ${wtd.average_weeks})`, key: "average", data: wtd.average,
                    borderColor: tokens.teamAverage, borderWidth: tokens.lineWidth, borderDash: [5, 4],
                    pointHoverBackgroundColor: tokens.teamAverage, order: 2,
                }),
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            spanGaps: false,
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: { color: tokens.axisText, font: { size: 11, weight: "600" }, boxWidth: 10, boxHeight: 2, padding: 14 },
                },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => wtd.days[items[0].dataIndex],
                    label: (item) => {
                        const value = item.raw;
                        if (value === null || value === undefined) return null;
                        if (item.dataset.key === "average") return `Average: ${value.toLocaleString("en-US")} by this day`;
                        return `This week: ${value} total (+${wtd.current_daily[item.dataIndex]} that day)`;
                    },
                }),
            },
        },
    });
}

function createMonthToDateChart(canvasEl, tokens) {
    const mtd = JSON.parse(canvasEl.dataset.mtd || "{}");
    const datasets = [
        {
            label: mtd.month_label,
            key: "current",
            data: mtd.current,
            borderColor: tokens.accentBlue,
            borderWidth: tokens.lineWidth + 1,
            backgroundColor: tokens.accentBlueBg,
            fill: true,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: tokens.accentBlue,
            pointHoverBorderWidth: 0,
            order: 1,
        },
        {
            label: mtd.prev_month_label,
            key: "previous",
            data: mtd.previous,
            borderColor: tokens.accentMagenta,
            borderWidth: tokens.lineWidth + 0.5,
            fill: false,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: tokens.accentMagenta,
            pointHoverBorderWidth: 0,
            order: 2,
        },
    ];
    if (mtd.ytd_months > 0) {
        datasets.push({
            label: `YTD Avg (${mtd.ytd_months} mo)`,
            key: "ytd",
            data: mtd.ytd_average,
            borderColor: tokens.teamAverage,
            borderWidth: tokens.lineWidth,
            borderDash: [5, 4],
            fill: false,
            tension: 0.25,
            pointRadius: 0,
            pointHoverRadius: 3,
            pointHoverBackgroundColor: tokens.teamAverage,
            pointHoverBorderWidth: 0,
            order: 3,
        });
    }

    const chart = new Chart(canvasEl, {
        type: "line",
        data: { labels: mtd.days, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            spanGaps: false,
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false }, ticks: { ...baseScaleOptions(tokens).ticks, maxTicksLimit: 11 } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: {
                        color: tokens.axisText,
                        font: { size: 11, weight: "600" },
                        boxWidth: 10,
                        boxHeight: 2,
                        padding: 14,
                    },
                },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => `Day ${mtd.days[items[0].dataIndex]}`,
                    label: (item) => {
                        const value = item.raw;
                        if (value === null || value === undefined) return null;
                        const key = item.dataset.key;
                        if (key === "ytd") return `YTD Avg: ${value.toLocaleString("en-US")} by this day`;
                        const daily = (key === "current" ? mtd.current_daily : mtd.previous_daily)[item.dataIndex];
                        const name = key === "current" ? mtd.month_short : mtd.prev_month_short;
                        return `${name}: ${value} total (+${daily} that day)`;
                    },
                }),
            },
        },
    });
    return chart;
}

// Wires the Monthly Sales card's two independent toggles -- range
// (12 Months / Month to Date) and view (Team/NJ/NY/VA). One panel per
// (view, range) pair ([data-trend-view][data-trend-range]); each chart is
// created lazily the first time its panel is shown, same reason as
// wireChartToggle() (a canvas created while display:none sizes to 0).
function wireMonthlyTrendCard(card) {
    if (!card) return;
    const rangeRoot = card.querySelector("[data-trend-range-toggle]");
    const viewRoot = card.querySelector("[data-trend-view-toggle]");
    const panels = card.querySelectorAll("[data-trend-view][data-trend-range]");
    const subtitles = card.querySelectorAll("[data-trend-subtitle]");
    const tokens = getChartTokens();
    const created = new Set();
    const state = { range: null, view: null };

    function render() {
        if (!state.range || !state.view) return;
        subtitles.forEach((el) => {
            el.style.display = el.dataset.trendSubtitle === state.range ? "" : "none";
        });
        panels.forEach((panel) => {
            const active = panel.dataset.trendView === state.view && panel.dataset.trendRange === state.range;
            panel.style.display = active ? "" : "none";
            const id = `${panel.dataset.trendView}:${panel.dataset.trendRange}`;
            if (active && !created.has(id)) {
                created.add(id);
                const canvas = panel.querySelector(":scope > canvas");
                const factory = canvas ? CHART_TOGGLE_FACTORIES[canvas.dataset.chartType] : null;
                if (factory) factory(canvas, tokens);
            }
        });
    }

    wireToggleButtons(rangeRoot, (key) => { state.range = key; render(); });
    wireToggleButtons(viewRoot, (key) => { state.view = key; render(); });
}

function createChannelChart(canvasEl, tokens) {
    const channels = JSON.parse(canvasEl.dataset.channels || "[]");
    const drilldownBase = canvasEl.dataset.drilldownBase;
    const total = channels.reduce((sum, c) => sum + c.count, 0);

    const chart = new Chart(canvasEl, {
        type: "bar",
        data: {
            labels: channels.map((c) => c.channel),
            datasets: [
                {
                    data: channels.map((c) => c.count),
                    backgroundColor: channels.map((_c, i) => tokens.channelPalette[i % tokens.channelPalette.length]),
                    borderRadius: tokens.barRadius,
                    borderSkipped: false,
                    maxBarThickness: 22,
                },
            ],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            onClick: (_evt, elements) => {
                if (!elements.length || !drilldownBase) return;
                const channel = channels[elements[0].index].channel;
                window.location.href = `${drilldownBase}&channel=${encodeURIComponent(channel)}`;
            },
            onHover: (evt, elements) => {
                evt.native.target.style.cursor = elements.length && drilldownBase ? "pointer" : "default";
            },
            scales: {
                x: { ...baseScaleOptions(tokens), beginAtZero: true },
                y: { ...baseScaleOptions(tokens), grid: { display: false } },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => channels[items[0].dataIndex].channel,
                    label: (item) => {
                        const count = channels[item.dataIndex].count;
                        const pct = total ? ((count / total) * 100).toFixed(0) : 0;
                        return [`${count} Sales`, `${pct}% of Total Sales`];
                    },
                }),
            },
        },
    });
    return chart;
}

function formatHourLabel(hour) {
    const h = Number(hour);
    if (h === 0) return "12a";
    if (h === 12) return "12p";
    return h < 12 ? `${h}a` : `${h - 12}p`;
}

function createHourlyChart(canvasEl, tokens) {
    const hours = JSON.parse(canvasEl.dataset.hours || "[]");

    const chart = new Chart(canvasEl, {
        type: "bar",
        data: {
            labels: hours.map((h) => formatHourLabel(h.hour)),
            datasets: [
                {
                    data: hours.map((h) => h.count),
                    backgroundColor: tokens.accentLime,
                    borderRadius: tokens.barRadius,
                    borderSkipped: false,
                    maxBarThickness: 16,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    title: (items) => formatHourLabel(hours[items[0].dataIndex].hour),
                    label: (item) => `${hours[item.dataIndex].count} Sales`,
                }),
            },
        },
    });
    return chart;
}

// Opacity a faded (non-focused) Sales Volume line fades down to on
// legend hover -- low enough to read as "de-emphasized" against the
// focused line + Team Average, high enough that its shape is still
// faintly traceable (never fully invisible -- no data disappears, only
// visual weight changes). See setSalesVolumeFocus() below.
const VOLUME_FADE_ALPHA = 0.12;

// Converts a dataset's base color (hex, e.g. "#3987e5", or an already-
// rgba() token like tokens.teamAverage) to an rgba() string at the given
// alpha -- used to fade/restore Sales Volume lines on legend hover
// without touching the underlying data, scales, or tooltip config.
function withAlpha(color, alpha) {
    if (color.charAt(0) === "#") {
        const hex = color.length === 4
            ? color.slice(1).split("").map((c) => c + c).join("")
            : color.slice(1);
        const value = parseInt(hex, 16);
        const r = (value >> 16) & 255;
        const g = (value >> 8) & 255;
        const b = value & 255;
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }
    const match = color.match(/rgba?\(([^)]+)\)/);
    if (match) {
        const [r, g, b] = match[1].split(",").map((part) => part.trim());
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }
    return color;
}

// Legend-hover focus for createSalesVolumeChart() -- fades every rep
// line except `focusedIndex` down to VOLUME_FADE_ALPHA, keeps Team
// Average (always) and the focused rep (if any) at full opacity, and
// mirrors the same emphasis onto the HTML legend items. focusedIndex
// null restores every line to normal (mouseleave). Only ever touches
// borderColor/backgroundColor -- never data, scales, or the click-to-
// toggle-visibility `hidden` state those are orthogonal to -- so no
// sales data, axis range, or tooltip behavior is affected, and a rep
// already hidden via legend click simply stays hidden. Relies on the
// chart's `animation.colors` option (set in createSalesVolumeChart()) so
// Chart.js itself tweens the color/alpha change smoothly instead of
// snapping, matching the rest of this dashboard's transition feel.
function setSalesVolumeFocus(chart, legendItems, focusedIndex) {
    chart.data.datasets.forEach((ds, i) => {
        const isTeamAverage = ds.label === "Team Average";
        const emphasized = focusedIndex === null || i === focusedIndex || isTeamAverage;
        const color = withAlpha(ds._baseColor, emphasized ? 1 : VOLUME_FADE_ALPHA);
        ds.borderColor = color;
        ds.backgroundColor = color;

        const item = legendItems[i];
        if (!item) return;
        item.classList.toggle("td-volume-legend-item-faded", focusedIndex !== null && i !== focusedIndex && !isTeamAverage);
        item.classList.toggle("td-volume-legend-item-focused", focusedIndex !== null && i === focusedIndex);
    });
    chart.update();
}

// Lightweight HTML legend for createSalesVolumeChart() below -- Chart.js's
// built-in canvas legend has no way to scroll, so a group with many reps
// would just keep growing the card taller. This renders into a plain DOM
// list instead (CSS gives it a max-height + scroll, see
// .td-volume-legend in team_dashboard.css) and reimplements the same
// click-to-toggle-a-dataset behavior Chart.js's own legend provides, plus
// a hover-to-focus interaction (desktop-only by design -- see
// setSalesVolumeFocus() above; touch devices keep only the existing
// tap-to-toggle-visibility behavior, since overloading the same tap for
// both would be the "complicated mobile interaction" the spec asked to
// avoid).
function renderVolumeLegend(container, chart) {
    if (!container) return;
    container.innerHTML = "";
    const items = [];
    chart.data.datasets.forEach((ds, i) => {
        const meta = chart.getDatasetMeta(i);
        ds._baseColor = ds.borderColor;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "td-volume-legend-item" + (meta.hidden ? " td-volume-legend-item-hidden" : "");
        const swatch = document.createElement("span");
        swatch.className = "td-volume-legend-swatch";
        swatch.style.background = ds.borderColor;
        item.appendChild(swatch);
        item.appendChild(document.createTextNode(ds.label));
        item.addEventListener("click", () => {
            meta.hidden = meta.hidden === null ? !ds.hidden : null;
            chart.update();
            item.classList.toggle("td-volume-legend-item-hidden", !!meta.hidden);
        });
        item.addEventListener("mouseenter", () => setSalesVolumeFocus(chart, items, i));
        item.addEventListener("mouseleave", () => setSalesVolumeFocus(chart, items, null));
        container.appendChild(item);
        items.push(item);
    });
}

function formatVolumeValue(value) {
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

// Multi-line cumulative-outbound-sales-per-rep chart for the Channel &
// Time-of-Day Performance section's Sales Volume tab (the section's
// default tab, added 2026-08-18). Data is pre-aggregated server-side by
// calculate_sales_volume_trend() -- reps sorted by total descending,
// each already a dense day-1..today cumulative series (no per-day
// recomputation needed here) -- plus a parallel Team Average series.
// Team Average is pushed in as its own dataset with order:0 so it draws
// (and legends/tooltips) on top of the rep lines, same "lower order
// number wins" convention createMonthlySalesChart() uses for its own
// cumulative overlay line.
function createSalesVolumeChart(canvasEl, tokens) {
    const dayLabels = JSON.parse(canvasEl.dataset.dayLabels || "[]");
    const reps = JSON.parse(canvasEl.dataset.reps || "[]");
    const teamAverage = JSON.parse(canvasEl.dataset.teamAverage || "[]");

    const repDatasets = reps.map((r, i) => {
        const color = tokens.channelPalette[i % tokens.channelPalette.length];
        return {
            label: r.rep,
            data: r.series,
            borderColor: color,
            backgroundColor: color,
            borderWidth: tokens.lineWidth,
            pointRadius: 0,
            pointHoverRadius: 3,
            pointHoverBackgroundColor: color,
            pointHoverBorderWidth: 0,
            tension: 0.25,
            order: 1,
        };
    });

    const teamAverageDataset = {
        label: "Team Average",
        data: teamAverage,
        borderColor: tokens.teamAverage,
        backgroundColor: tokens.teamAverage,
        borderWidth: tokens.lineWidth + 1.5,
        borderDash: [7, 4],
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBackgroundColor: tokens.teamAverage,
        pointHoverBorderWidth: 0,
        tension: 0.25,
        order: 0,
    };

    const chart = new Chart(canvasEl, {
        type: "line",
        data: {
            labels: dayLabels,
            datasets: [teamAverageDataset, ...repDatasets],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            // Chart.js v4 already ships a default named "colors"
            // animation collection covering borderColor/backgroundColor
            // -- restated explicitly here (matching baseAnimation()'s
            // duration/easing) so it's obvious this is what makes
            // setSalesVolumeFocus()'s color/alpha changes tween smoothly
            // on chart.update() (a "premium" opacity fade) rather than
            // snapping instantly, instead of relying silently on an
            // unstated library default.
            animations: { colors: { type: "color", duration: 400, easing: "easeOutQuart", properties: ["borderColor", "backgroundColor"] } },
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true, ticks: { ...baseScaleOptions(tokens).ticks, precision: 0 } },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    ...buildTooltipConfig(tokens, {
                        title: (items) => dayLabels[items[0].dataIndex],
                        label: (item) => `${item.dataset.label} — ${formatVolumeValue(item.parsed.y)}`,
                    }),
                    displayColors: true,
                    boxWidth: 8,
                    boxHeight: 8,
                    itemSort: (a, b) => {
                        if (a.dataset.label === "Team Average") return 1;
                        if (b.dataset.label === "Team Average") return -1;
                        return b.parsed.y - a.parsed.y;
                    },
                },
            },
        },
    });

    renderVolumeLegend(document.getElementById(canvasEl.dataset.legendTarget), chart);
    return chart;
}

// Factory lookup for wireChartToggle() below, keyed by each canvas's
// data-chart-type attribute -- lets one generic toggle handler drive any
// pair of charts without hardcoding canvas ids. "monthlyTrend" backs the
// Sales Calendar & Monthly Sales Trend section's Team/NJ/NY/VA toggle
// (added 2026-08-18) -- same factory as the plain single-chart usage,
// just invoked once per state's canvas instead of once for the page.
const CHART_TOGGLE_FACTORIES = {
    channel: createChannelChart,
    hourly: createHourlyChart,
    monthlyTrend: createMonthlySalesTrendChart,
    monthToDate: createMonthToDateChart,
    weekToDate: createWeekToDateChart,
    rateTrend: createRateTrendChart,
    monthlyStatus: createMonthlyStatusChart,
    statusCompare: createStatusCompareChart,
    salesVolume: createSalesVolumeChart,
    bucketSeries: createBucketSeriesChart,
};

// Shared button-wiring for a .td-chart-toggle segmented control: one
// .td-chart-toggle-btn per tab (data-chart-toggle="<key>"), clicking one
// marks it active and calls onActivate(key) so the caller can show/hide
// panels (and, for chart toggles, lazily create a chart). The initially
// active tab is whichever button the server already marked
// .td-chart-toggle-active (so Prev/Next month nav on the Sales Calendar
// can carry the selected view forward across a full page reload); falls
// back to the first button if the server didn't mark one.
function wireToggleButtons(root, onActivate) {
    if (!root) return;
    const buttons = root.querySelectorAll(".td-chart-toggle-btn");
    if (!buttons.length) return;

    function activate(key) {
        buttons.forEach((btn) => {
            btn.classList.toggle("td-chart-toggle-active", btn.dataset.chartToggle === key);
        });
        onActivate(key);
    }

    buttons.forEach((btn) => {
        btn.addEventListener("click", () => activate(btn.dataset.chartToggle));
    });

    const initialBtn = root.querySelector(".td-chart-toggle-btn.td-chart-toggle-active") || buttons[0];
    activate(initialBtn.dataset.chartToggle);
}

// Wires a chart-backed toggle (Sales Volume / Channel Performance / Time
// of Day, the Sales Calendar & Monthly Sales Trend's Team/NJ/NY/VA trend
// toggle, or the Sales Volume tab's own nested Team/NJ/NY/VA group
// toggle): one [data-chart-panel="<key>"] wrapper per chart. Only one
// panel is shown at a time; each chart is created lazily the first time
// its tab is activated (a Chart.js canvas created while display:none
// sizes to 0 and won't self-correct later, so the default-active tab's
// chart is created immediately and the rest are deferred until their
// first click).
//
// scopeSelector defaults to ".td-chart-card" (the outer Sales Volume /
// Channel Performance / Time of Day card). The Sales Volume tab nests a
// second toggle+panel set (Team/NJ/NY/VA) inside that same card, so it
// passes ".td-volume-group" instead -- root.closest() finds the nearer
// ancestor either way, keeping the two toggles' [data-chart-panel]
// elements from colliding. The lazy-create lookup below only ever looks
// for a canvas that's a *direct* child of its panel (":scope > canvas"),
// so the outer toggle's "volume" panel -- which contains the nested
// toggle's own canvases, not one of its own -- is correctly left for
// that nested wireChartToggle() call to create instead of double-firing
// a factory on the first canvas it happens to find nested inside.
function wireChartToggle(root, scopeSelector) {
    if (!root) return;
    const card = root.closest(scopeSelector || ".td-chart-card") || root.parentElement;
    const panels = card.querySelectorAll(":scope > [data-chart-panel]");
    const created = new Set();
    const tokens = getChartTokens();

    wireToggleButtons(root, (key) => {
        panels.forEach((panel) => {
            panel.style.display = panel.dataset.chartPanel === key ? "" : "none";
        });
        if (!created.has(key)) {
            created.add(key);
            const panel = card.querySelector(`:scope > [data-chart-panel="${key}"]`);
            const canvas = panel ? panel.querySelector(":scope > canvas") : null;
            const factory = canvas ? CHART_TOGGLE_FACTORIES[canvas.dataset.chartType] : null;
            if (factory) factory(canvas, tokens);
        }
    });
}

// Wires a plain panel toggle with no chart involved -- the Sales
// Calendar's Team/NJ/NY/VA toggle (added 2026-08-18). Same show/hide
// behavior as wireChartToggle() minus the lazy chart creation, since
// every panel here is just pre-rendered calendar markup.
function wireCalendarToggle(root) {
    if (!root) return;
    const card = root.closest(".td-calendar-card") || root.parentElement;
    const panels = card.querySelectorAll("[data-chart-panel]");

    wireToggleButtons(root, (key) => {
        panels.forEach((panel) => {
            panel.style.display = panel.dataset.chartPanel === key ? "" : "none";
        });
    });
}

// ================================================================
// Passings & Leads chart factories -- 4 generic factories reused across
// the page's 8 chart cards (see templates/passings.html) rather than one
// bespoke function per card, since every card is one of: a single time
// series, two time series overlaid, a small category breakdown, or a
// ranked bar list. Each reads its data from data-* attributes on its
// <canvas>, same |tojson convention every other chart in this file uses.
// ================================================================

function createBucketSeriesChart(canvasEl, tokens) {
    const buckets = JSON.parse(canvasEl.dataset.buckets || "[]");
    const style = canvasEl.dataset.chartStyle === "line" ? "line" : "bar";
    const color = tokens[canvasEl.dataset.color] || tokens.accentBlue;
    const colorBg = tokens[canvasEl.dataset.color + "Bg"] || tokens.accentBlueBg;
    const label = canvasEl.dataset.seriesLabel || "Value";

    const chart = new Chart(canvasEl, {
        type: style,
        data: {
            labels: buckets.map((b) => b.label),
            datasets: [{
                label,
                data: buckets.map((b) => b.value),
                backgroundColor: style === "bar" ? color : colorBg,
                borderColor: color,
                borderRadius: style === "bar" ? tokens.barRadius : undefined,
                borderSkipped: false,
                maxBarThickness: 22,
                borderWidth: style === "line" ? tokens.lineWidth + 1 : undefined,
                fill: style === "line",
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    label: (item) => `${label}: ${item.formattedValue}`,
                }),
            },
        },
    });
    return chart;
}

function createDualBucketChart(canvasEl, tokens) {
    const buckets = JSON.parse(canvasEl.dataset.buckets || "[]");
    const labelA = canvasEl.dataset.labelA || "Series A";
    const labelB = canvasEl.dataset.labelB || "Series B";

    const chart = new Chart(canvasEl, {
        type: "bar",
        data: {
            labels: buckets.map((b) => b.label),
            datasets: [
                {
                    label: labelA,
                    data: buckets.map((b) => b.a),
                    backgroundColor: tokens.accentBlue,
                    borderRadius: tokens.barRadius,
                    borderSkipped: false,
                    maxBarThickness: 18,
                    order: 2,
                },
                {
                    label: labelB,
                    data: buckets.map((b) => b.b),
                    type: "line",
                    borderColor: tokens.accentLime,
                    backgroundColor: tokens.accentLime,
                    borderWidth: tokens.lineWidth + 1,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    tension: 0.3,
                    order: 1,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            interaction: { mode: "index", intersect: false },
            scales: {
                x: { ...baseScaleOptions(tokens), grid: { display: false } },
                y: { ...baseScaleOptions(tokens), beginAtZero: true },
            },
            plugins: {
                legend: {
                    display: true,
                    labels: { color: tokens.axisText, boxWidth: 10, font: { size: 11 } },
                },
                tooltip: buildTooltipConfig(tokens, {}),
            },
        },
    });
    return chart;
}

function createDonutChart(canvasEl, tokens) {
    const labels = JSON.parse(canvasEl.dataset.labels || "[]");
    const values = JSON.parse(canvasEl.dataset.values || "[]");
    const colorKeys = JSON.parse(canvasEl.dataset.colors || "[]");
    const colors = colorKeys.map((key) => tokens[key] || tokens.accentBlue);

    const chart = new Chart(canvasEl, {
        type: "doughnut",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: colors,
                borderColor: tokens.chartBg || "transparent",
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            cutout: "62%",
            plugins: {
                legend: {
                    display: true,
                    position: "bottom",
                    labels: { color: tokens.axisText, boxWidth: 10, font: { size: 11 }, padding: 12 },
                },
                tooltip: buildTooltipConfig(tokens, {
                    label: (item) => {
                        const total = values.reduce((sum, v) => sum + v, 0);
                        const pct = total ? ((item.raw / total) * 100).toFixed(1) : "0.0";
                        return `${item.label}: ${item.formattedValue} (${pct}%)`;
                    },
                }),
            },
        },
    });
    return chart;
}

function createHorizontalBarChart(canvasEl, tokens) {
    const labels = JSON.parse(canvasEl.dataset.labels || "[]");
    const values = JSON.parse(canvasEl.dataset.values || "[]");
    const suffix = canvasEl.dataset.suffix || "";
    const color = tokens[canvasEl.dataset.color] || tokens.accentBlue;

    const chart = new Chart(canvasEl, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: color,
                borderRadius: tokens.barRadius,
                borderSkipped: false,
                maxBarThickness: 18,
            }],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            animation: baseAnimation(),
            scales: {
                x: { ...baseScaleOptions(tokens), beginAtZero: true },
                y: { ...baseScaleOptions(tokens), grid: { display: false } },
            },
            plugins: {
                legend: { display: false },
                tooltip: buildTooltipConfig(tokens, {
                    label: (item) => `${item.formattedValue}${suffix}`,
                }),
            },
        },
    });
    return chart;
}

function wireVisibleCalendarAndTrendCards() {
    document.querySelectorAll("[data-calendar-toggle], [data-trend-card]").forEach((el) => {
        if (el.dataset.wired || el.closest("[data-kpi-follow][hidden]")) return;
        el.dataset.wired = "1";
        if (el.hasAttribute("data-calendar-toggle")) wireCalendarToggle(el);
        else wireMonthlyTrendCard(el);
    });
}

// Fired by team_dashboard.js when the overview Team / Door Knockers /
// Total toggle changes (after it un-hides the matching panels).
document.addEventListener("td:kpi-view", () => {
    if (typeof Chart !== "undefined" && document.readyState !== "loading") wireVisibleCalendarAndTrendCards();
});

document.addEventListener("DOMContentLoaded", () => {
    if (typeof Chart === "undefined") return;
    const tokens = getChartTokens();

    // Rep Profile Monthly Sales card (By Month / one month by day, added
    // 2026-10-26): the day chart lives in a hidden panel there, so it's
    // created when first shown instead of here. Remembers the last tab.
    const repSalesToggle = document.getElementById("rep-sales-chart-toggle");
    const monthlyEl = document.getElementById("monthly-sales-chart");
    if (monthlyEl && !repSalesToggle) createMonthlySalesChart(monthlyEl, tokens);
    if (repSalesToggle) {
        let saved = null;
        try { saved = localStorage.getItem("td-rep-sales-chart"); } catch (e) { /* storage unavailable */ }
        const savedBtn = saved && repSalesToggle.querySelector(`[data-chart-toggle="${saved}"]`);
        if (savedBtn) {
            repSalesToggle.querySelectorAll(".td-chart-toggle-btn").forEach((b) => b.classList.toggle("td-chart-toggle-active", b === savedBtn));
        }
        const card = repSalesToggle.closest(".td-chart-card");
        let dayChart = false;
        wireToggleButtons(repSalesToggle, (key) => {
            card.querySelectorAll("[data-chart-panel]").forEach((el) => {
                el.style.display = el.dataset.chartPanel === key ? "" : "none";
            });
            if (key === "by_month") {
                const el = document.getElementById("rep-sales-by-month-chart");
                if (el && !el.dataset.created) { el.dataset.created = "1"; createMonthlySalesTrendChart(el, tokens); }
            } else if (monthlyEl && !dayChart) {
                dayChart = true;
                createMonthlySalesChart(monthlyEl, tokens);
            }
            try { localStorage.setItem("td-rep-sales-chart", key); } catch (e) { /* storage unavailable */ }
        });
    }

    document.querySelectorAll('canvas[data-chart-type="blitzDaily"]').forEach((el) => createBlitzDailyChart(el, tokens));
    document.querySelectorAll('canvas[data-chart-type="statusStacked"]').forEach((el) => createStatusStackedChart(el, tokens));

    wireChartToggle(document.getElementById("channel-hourly-toggle"));
    wireChartToggle(document.getElementById("team-channel-hourly-toggle"));
    // Sales Calendar & Monthly Sales Trend: wire every calendar/trend card
    // that's visible now. Copies inside a hidden [data-kpi-follow]
    // dataset panel (Team Leaderboard: Door Knockers / Total) are wired
    // the first time they're shown -- see the td:kpi-view listener below
    // (a chart created while hidden would size to 0).
    wireVisibleCalendarAndTrendCards();
    wireChartToggle(document.getElementById("knocker-rates-toggle"));
    wireChartToggle(document.getElementById("knocker-monthly-status-toggle"));
    wireChartToggle(document.getElementById("team-monthly-status-toggle"));
    // Sales Volume tab's own nested Team/NJ/NY/VA toggle -- wired
    // unconditionally like team-monthly-trend-toggle above, not lazily
    // via the outer toggle, since Sales Volume is the outer toggle's
    // default-active tab (so its default "team" panel is already visible
    // -- not display:none -- by the time this runs, same precondition
    // team-monthly-trend-toggle already relies on).
    wireChartToggle(document.getElementById("team-sales-volume-group-toggle"), ".td-volume-group");

    // Team Outbound Calls (dashboard.html) -- generic line series, one
    // panel per time-scale tab. The head's per-tab call-count subtitles
    // are [data-chart-panel] too but aren't direct card children, so
    // they're toggled separately here.
    const teamCallsToggle = document.getElementById("team-calls-scale-toggle");
    wireChartToggle(teamCallsToggle);
    if (teamCallsToggle) {
        const head = teamCallsToggle.closest(".td-chart-card-head");
        teamCallsToggle.querySelectorAll(".td-chart-toggle-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                head.querySelectorAll("[data-chart-panel]").forEach((el) => {
                    el.style.display = el.dataset.chartPanel === btn.dataset.chartToggle ? "" : "none";
                });
            });
        });
    }

    // Passings & Leads (templates/passings.html) -- every chart on that
    // page is one of these 4 generic factories, looked up by a shared
    // [data-passing-chart] marker rather than one getElementById per
    // canvas, since there are 8 of them.
    document.querySelectorAll("[data-passing-chart]").forEach((canvasEl) => {
        const kind = canvasEl.dataset.passingChart;
        if (kind === "bucket-series") createBucketSeriesChart(canvasEl, tokens);
        else if (kind === "dual-bucket") createDualBucketChart(canvasEl, tokens);
        else if (kind === "donut") createDonutChart(canvasEl, tokens);
        else if (kind === "horizontal-bar") createHorizontalBarChart(canvasEl, tokens);
    });
});
