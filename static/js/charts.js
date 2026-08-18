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
        accentBlue: read("--td-accent-blue"),
        accentBlueBg: read("--td-accent-blue-bg"),
        accentLime: read("--td-accent-lime"),
        channelPalette: [
            read("--td-chart-channel-1"),
            read("--td-chart-channel-2"),
            read("--td-chart-channel-3"),
            read("--td-chart-channel-4"),
            read("--td-chart-channel-5"),
            read("--td-chart-channel-6"),
            read("--td-chart-channel-7"),
            read("--td-chart-channel-8"),
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

// Wires a chart-backed toggle (Sales by Channel / Sales by Hour, or the
// Sales Calendar & Monthly Sales Trend's Team/NJ/NY/VA trend toggle):
// one [data-chart-panel="<key>"] wrapper per chart. Only one panel is
// shown at a time; each chart is created lazily the first time its tab
// is activated (a Chart.js canvas created while display:none sizes to 0
// and won't self-correct later, so the default-active tab's chart is
// created immediately and the rest are deferred until their first click).
function wireChartToggle(root) {
    if (!root) return;
    const card = root.closest(".td-chart-card") || root.parentElement;
    const panels = card.querySelectorAll("[data-chart-panel]");
    const created = new Set();
    const tokens = getChartTokens();

    wireToggleButtons(root, (key) => {
        panels.forEach((panel) => {
            panel.style.display = panel.dataset.chartPanel === key ? "" : "none";
        });
        if (!created.has(key)) {
            created.add(key);
            const panel = card.querySelector(`[data-chart-panel="${key}"]`);
            const canvas = panel ? panel.querySelector("canvas") : null;
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

document.addEventListener("DOMContentLoaded", () => {
    if (typeof Chart === "undefined") return;
    const tokens = getChartTokens();

    const monthlyEl = document.getElementById("monthly-sales-chart");
    if (monthlyEl) createMonthlySalesChart(monthlyEl, tokens);

    wireChartToggle(document.getElementById("channel-hourly-toggle"));
    wireChartToggle(document.getElementById("team-channel-hourly-toggle"));
    wireCalendarToggle(document.getElementById("team-calendar-toggle"));
    wireChartToggle(document.getElementById("team-monthly-trend-toggle"));
});
