/* Parcel Map (added 2026-11-05). Draws the NJ parcels in view from the
   state's public service, and turns a parcel green when a passing that
   became available inside the selected dates falls inside it. Server side:
   parcel_map.py. */
(function () {
    "use strict";

    const el = document.getElementById("parcel-map");
    const statusEl = document.getElementById("parcel-map-status");
    if (!el || !window.L) return;

    const MIN_PARCEL_ZOOM = 15;
    const PAGE_SIZE = 2000;
    const MAX_PAGES = 6;
    const MAX_LOADED = 12000;
    const CELL = 0.002; // degrees; passing points are bucketed to this grid
    const OUT_FIELDS = "OBJECTID,PCLBLOCK,PCLLOT,PROP_LOC,MUN_NAME,CALC_ACRE";

    const map = L.map(el, { preferCanvas: true, minZoom: 7, maxZoom: 19 }).setView([40.2, -74.6], 8);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · Parcels: NJOGIS',
    }).addTo(map);

    let points = [];
    const buckets = new Map();
    const cellKey = (lat, lng) => Math.floor(lat / CELL) + ":" + Math.floor(lng / CELL);

    const setStatus = (text) => { statusEl.textContent = text; };

    /* ---- Which passings sit inside a parcel. ---- */
    function polygonsOf(geometry) {
        if (!geometry) return [];
        if (geometry.type === "Polygon") return [geometry.coordinates];
        if (geometry.type === "MultiPolygon") return geometry.coordinates;
        return [];
    }

    // Even-odd across every ring, so holes in a parcel don't count as inside.
    function insidePolygon(lng, lat, rings) {
        let inside = false;
        for (const ring of rings) {
            for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                const [xi, yi] = ring[i];
                const [xj, yj] = ring[j];
                if ((yi > lat) !== (yj > lat) && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
                    inside = !inside;
                }
            }
        }
        return inside;
    }

    function passingsIn(geometry) {
        const found = [];
        for (const rings of polygonsOf(geometry)) {
            let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
            for (const [x, y] of rings[0]) {
                minX = Math.min(minX, x); maxX = Math.max(maxX, x);
                minY = Math.min(minY, y); maxY = Math.max(maxY, y);
            }
            for (let a = Math.floor(minY / CELL); a <= Math.floor(maxY / CELL); a++) {
                for (let b = Math.floor(minX / CELL); b <= Math.floor(maxX / CELL); b++) {
                    for (const p of buckets.get(a + ":" + b) || []) {
                        if (insidePolygon(p[2], p[1], rings)) found.push(p);
                    }
                }
            }
        }
        return found;
    }

    /* ---- Availability dates: slider and date boxes stay in step. ---- */
    const dateFrom = document.getElementById("parcel-date-from");
    const dateTo = document.getElementById("parcel-date-to");
    const sliderFrom = document.getElementById("parcel-slider-from");
    const sliderTo = document.getElementById("parcel-slider-to");
    const sliderFill = document.getElementById("parcel-slider-fill");
    const DAY_MS = 86400000;
    const toDayNumber = (iso) => Date.parse(iso + "T00:00:00Z") / DAY_MS;
    const toIso = (dayNumber) => new Date(dayNumber * DAY_MS).toISOString().slice(0, 10);

    let firstDay = 0;
    let lastDay = 0;
    let fromDay = 0;
    let toDay = 0;

    // Passings with no date only count while every date is selected.
    // p[7] is the availability day, added in setUpDateRange().
    function inDateRange(passing) {
        if (fromDay === firstDay && toDay === lastDay) return true;
        return passing[7] !== null && passing[7] >= fromDay && passing[7] <= toDay;
    }

    // Organic sales made in these dates on the passings available in these
    // dates, over those passings. Covers all of NJ, not just the map in view.
    function showOrganicRate() {
        let passings = 0;
        let sales = 0;
        for (const p of points) {
            if (!inDateRange(p)) continue;
            passings += 1;
            for (const day of p[4]) {
                if (day >= fromDay && day <= toDay) sales += 1;
            }
        }
        document.getElementById("parcel-kpi-passings").textContent = passings.toLocaleString();
        document.getElementById("parcel-kpi-sales").textContent = sales.toLocaleString();
        document.getElementById("parcel-kpi-rate").textContent = passings ? ((sales / passings) * 100).toFixed(1) + "%" : "—";
    }

    /* ---- Organic curve. For each month or week since available: of the
       passings with no order yet, the share that ordered. A passing counts
       for a period only when all of it passed before today and before knockers reached
       its town. With the after-knockers view on, it counts until a knocker
       sells that house instead, so those months include knocker effects.
       Available means the first day it could be sold, Pre-Order included.
       Passings ordered before that aren't counted. ---- */
    const MIN_PASSINGS = 100;   // fewer in a period is too noisy to plot
    // Both scales cover the same two years. band is in points; a week's rate
    // is about a quarter of a month's, so its band and decimals scale with it.
    // run = how many periods in a row must stay within the band. shown = how
    // many periods the chart draws: weeks are the short view, while the
    // plateau behind the cards still uses the full two years.
    const CURVE_SCALES = {
        month: { name: "Month", days: 30.4375, periods: 25, plateauFrom: 12, minPlateauPeriods: 6, band: 0.2, run: 3, decimals: 1, shown: 25 },
        week: { name: "Week", days: 7, periods: 105, plateauFrom: 52, minPlateauPeriods: 26, band: 0.05, run: 13, decimals: 2, shown: 27 },
    };

    // What the toggles were set to, and what's on screen. They differ when the
    // dates are too short for the chosen view; see pickCurveView().
    let chosenScale = CURVE_SCALES.month;
    let chosenAfterKnockers = false;
    let curveScale = chosenScale;
    let includeAfterKnockers = chosenAfterKnockers;
    // A toggle click always wins over pickCurveView() until the dates change.
    let keepChosenView = false;

    function organicCurve() {
        const atRisk = new Array(curveScale.periods).fill(0);
        const ordered = new Array(curveScale.periods).fill(0);
        for (const p of points) {
            if (p[7] === null || !inDateRange(p)) continue;
            const firstOrder = p[10];
            if (firstOrder !== null && firstOrder < p[7]) continue;
            const stop = includeAfterKnockers ? p[9] : p[8];
            const days = Math.min(lastDay, stop === null ? lastDay : stop) - p[7];
            for (let period = 0; period < curveScale.periods; period++) {
                const periodEnd = (period + 1) * curveScale.days;
                if (periodEnd > days) break;
                atRisk[period] += 1;
                if (firstOrder !== null && firstOrder - p[7] < periodEnd) {
                    ordered[period] += 1;
                    break;
                }
            }
        }
        return { atRisk, ordered };
    }

    function plateauRate(curve) {
        let periods = 0, atRisk = 0, ordered = 0;
        for (let period = curveScale.plateauFrom; period < curveScale.periods; period++) {
            if (curve.atRisk[period] < MIN_PASSINGS) continue;
            periods += 1;
            atRisk += curve.atRisk[period];
            ordered += curve.ordered[period];
        }
        return periods >= curveScale.minPlateauPeriods ? (ordered / atRisk) * 100 : null;
    }

    // First period whose run, averaged, sits within the band of the plateau.
    function decayEnd(rates, plateau) {
        if (plateau === null) return null;
        const { run, band, periods } = curveScale;
        for (let period = 1; period <= periods - run; period++) {
            const next = rates.slice(period, period + run);
            if (next.some((rate) => rate === null)) continue;
            const average = next.reduce((sum, rate) => sum + rate, 0) / run;
            if (Math.abs(average - plateau) <= band && Math.abs(next[0] - plateau) <= band * 3) return period;
        }
        return null;
    }

    let curveChart = null;
    let curveCounts = null;

    function drawCurve(rates, plateau) {
        const style = getComputedStyle(document.documentElement);
        const token = (name) => style.getPropertyValue(name).trim();
        const shownRates = rates.slice(0, curveScale.shown);
        const plateauLine = shownRates.map(() => plateau);
        const labels = shownRates.map((_, period) => period);
        const unit = curveScale.name.toLowerCase();
        const markerSize = 4;
        if (curveChart) {
            curveChart.data.labels = labels;
            curveChart.data.datasets[0].data = shownRates;
            curveChart.data.datasets[1].data = plateauLine;
            curveChart.options.scales.x.title.text = curveScale.name + "s since available";
            curveChart.options.scales.y.title.text = "Ordering that " + unit;
            curveChart.update("none");
            return;
        }
        const axis = { color: token("--td-text-secondary") };
        curveChart = new Chart(document.getElementById("parcel-curve-chart"), {
            type: "line",
            data: {
                labels,
                datasets: [
                    {
                        label: "Organic order rate", data: shownRates, borderColor: token("--td-cyan"), backgroundColor: token("--td-cyan"),
                        borderWidth: 2, pointRadius: markerSize, pointHoverRadius: 6, spanGaps: false,
                    },
                    {
                        label: "Plateau", data: plateauLine, borderColor: token("--td-purple"), backgroundColor: token("--td-purple"),
                        borderWidth: 2, borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    x: { title: { display: true, text: curveScale.name + "s since available", ...axis }, ticks: { ...axis, maxTicksLimit: 14 }, grid: { display: false } },
                    y: {
                        beginAtZero: true,
                        title: { display: true, text: "Ordering that " + unit, ...axis },
                        ticks: { ...axis, callback: (value) => value + "%" },
                        grid: { color: token("--td-chart-grid") },
                    },
                },
                plugins: {
                    legend: { labels: { color: token("--td-text-secondary"), boxHeight: 2 } },
                    tooltip: {
                        filter: (item) => item.datasetIndex === 0,
                        callbacks: {
                            title: (items) => curveScale.name + " " + items[0].label,
                            label: (item) => {
                                const period = item.dataIndex;
                                return item.parsed.y.toFixed(curveScale.decimals) + "% · " + curveCounts.ordered[period].toLocaleString() +
                                    " of " + curveCounts.atRisk[period].toLocaleString() + " passings";
                            },
                        },
                    },
                },
            },
        });
    }

    function plottedPeriods(curve) {
        return curve.atRisk.slice(0, curveScale.shown).filter((atRisk) => atRisk >= MIN_PASSINGS).length;
    }

    // Short date ranges often have no full month yet, and new passings are
    // mostly in towns knockers already reached. Uses the chosen view when it
    // can draw a line, otherwise the closest view that draws the most.
    function pickCurveView() {
        if (keepChosenView) {
            curveScale = chosenScale;
            includeAfterKnockers = chosenAfterKnockers;
            return organicCurve();
        }
        const views = [
            [chosenScale, chosenAfterKnockers],
            [chosenScale, true],
            [CURVE_SCALES.week, chosenAfterKnockers],
            [CURVE_SCALES.week, true],
        ];
        let best = null;
        for (const [scale, afterKnockers] of views) {
            curveScale = scale;
            includeAfterKnockers = afterKnockers;
            const curve = organicCurve();
            const plotted = plottedPeriods(curve);
            if (!best || plotted > best.plotted) best = { scale, afterKnockers, curve, plotted };
            if (plotted >= 2) break;
        }
        curveScale = best.scale;
        includeAfterKnockers = best.afterKnockers;
        return best.curve;
    }

    function showCurveControls() {
        for (const button of document.querySelectorAll("[data-curve-scale]")) {
            const active = CURVE_SCALES[button.dataset.curveScale] === curveScale;
            button.classList.toggle("td-chart-toggle-active", active);
            button.setAttribute("aria-pressed", String(active));
        }
        for (const button of document.querySelectorAll("[data-curve-knockers]")) {
            const active = (button.dataset.curveKnockers === "after") === includeAfterKnockers;
            button.classList.toggle("td-chart-toggle-active", active);
            button.setAttribute("aria-pressed", String(active));
        }
        document.getElementById("parcel-curve-rule").textContent = includeAfterKnockers
            ? "Includes time after door knockers reached the town, so knockers may lower these rates. A passing drops out once a knocker sells it."
            : "A passing drops out once door knockers reach its town.";
        const switched = [];
        if (curveScale !== chosenScale) switched.push("weeks, because these passings haven't had a full month yet");
        if (includeAfterKnockers !== chosenAfterKnockers) switched.push("time after knockers arrived, because knockers had already reached most of these towns");
        const note = document.getElementById("parcel-curve-note");
        note.textContent = "These dates are too short for the view you picked. Showing " + switched.join(", and ") + ".";
        note.hidden = !switched.length;
    }

    function showOrganicCurve() {
        const curve = pickCurveView();
        curveCounts = curve;
        showCurveControls();
        const rates = curve.atRisk.map((atRisk, period) =>
            atRisk >= MIN_PASSINGS ? (curve.ordered[period] / atRisk) * 100 : null
        );
        const plateau = plateauRate(curve);
        const end = decayEnd(rates, plateau);
        const percent = (rate) => (rate === null ? "—" : rate.toFixed(curveScale.decimals) + "%");
        const { name } = curveScale;
        const unit = name.toLowerCase();
        document.getElementById("parcel-curve-title").textContent = "Organic Orders by " + name + " Since Available";
        document.getElementById("parcel-curve-period").textContent = unit;
        document.getElementById("parcel-curve-first-label").textContent = "Opening " + name;
        document.getElementById("parcel-curve-first-note").textContent = unit + " 0 order rate";
        document.getElementById("parcel-curve-start-note").textContent = curveScale.run + " " + unit + "s average near plateau";
        document.getElementById("parcel-curve-plateau-note").textContent =
            "per " + unit + ", " + unit + "s " + curveScale.plateauFrom + "–" + (curveScale.periods - 1);
        document.getElementById("parcel-curve-first").textContent = percent(rates[0]);
        document.getElementById("parcel-curve-plateau").textContent = percent(plateau);
        document.getElementById("parcel-curve-start").textContent = end === null ? "—" : name + " " + end;
        const hasCurve = rates.slice(0, curveScale.shown).some((rate) => rate !== null);
        const empty = document.getElementById("parcel-curve-empty");
        empty.hidden = hasCurve;
        if (!includeAfterKnockers) {
            empty.textContent = "No data for Before Knockers in these dates: knockers had already reached these towns. Use Include After Knockers to see them.";
        } else if (curveScale === CURVE_SCALES.month) {
            empty.textContent = "These passings haven't had a full month yet. Use Weeks to see them.";
        } else {
            empty.textContent = "Not enough history in these dates to draw the curve.";
        }
        document.querySelector(".td-parcel-curve-chart").hidden = !hasCurve;
        if (hasCurve && window.Chart) drawCurve(rates, plateau);
    }

    for (const button of document.querySelectorAll("[data-curve-scale]")) {
        button.addEventListener("click", () => {
            chosenScale = CURVE_SCALES[button.dataset.curveScale];
            chosenAfterKnockers = includeAfterKnockers;
            keepChosenView = true;
            if (points.length) showOrganicCurve();
        });
    }

    for (const button of document.querySelectorAll("[data-curve-knockers]")) {
        button.addEventListener("click", () => {
            chosenAfterKnockers = button.dataset.curveKnockers === "after";
            chosenScale = curveScale;
            keepChosenView = true;
            if (points.length) showOrganicCurve();
        });
    }

    let repaint = null;
    function repaintParcels() {
        if (repaint) return;
        repaint = requestAnimationFrame(() => {
            repaint = null;
            showOrganicRate();
            showOrganicCurve();
            for (const parcel of loaded.values()) {
                parcel.layer.setStyle(isShaded(parcel.passings) ? STYLE_PASSING : STYLE_OTHER);
            }
            if (map.getZoom() >= MIN_PARCEL_ZOOM && loaded.size) showParcelCount(lastCapped);
        });
    }

    function setDateRange(from, to) {
        keepChosenView = false;
        fromDay = Math.max(firstDay, Math.min(from, lastDay));
        toDay = Math.max(fromDay, Math.min(to, lastDay));
        sliderFrom.value = fromDay - firstDay;
        sliderTo.value = toDay - firstDay;
        dateFrom.value = toIso(fromDay);
        dateTo.value = toIso(toDay);
        const span = Math.max(lastDay - firstDay, 1);
        sliderFill.style.left = ((fromDay - firstDay) / span) * 100 + "%";
        sliderFill.style.right = ((lastDay - toDay) / span) * 100 + "%";
        repaintParcels();
    }

    function setUpDateRange() {
        for (const p of points) {
            p[4] = p[4].map(toDayNumber);
            p[7] = p[3] ? toDayNumber(p[3]) : null;
            p[8] = p[5] ? toDayNumber(p[5]) : null;
            p[9] = p[6] ? toDayNumber(p[6]) : null;
            p[10] = p[4].length ? Math.min(...p[4]) : null;
        }
        const dates = points.map((p) => p[3]).filter(Boolean).sort();
        if (!dates.length) return;
        firstDay = toDayNumber(dates[0]);
        lastDay = toDayNumber(dates[dates.length - 1]);
        // Runs to the latest sale too, so the newest sales are in range.
        for (const p of points) {
            for (const day of p[4]) lastDay = Math.max(lastDay, day);
        }
        for (const slider of [sliderFrom, sliderTo]) {
            slider.min = 0;
            slider.max = lastDay - firstDay;
        }
        for (const input of [dateFrom, dateTo]) {
            input.min = toIso(firstDay);
            input.max = toIso(lastDay);
        }
        for (const control of [dateFrom, dateTo, sliderFrom, sliderTo]) control.disabled = false;
        setDateRange(firstDay, lastDay);
    }

    // The handles can meet but not cross.
    sliderFrom.addEventListener("input", () => {
        setDateRange(Math.min(firstDay + Number(sliderFrom.value), toDay), toDay);
    });
    sliderTo.addEventListener("input", () => {
        setDateRange(fromDay, Math.max(firstDay + Number(sliderTo.value), fromDay));
    });
    // A From after To moves To along with it, and the reverse. An empty box
    // goes back to the current date.
    dateFrom.addEventListener("change", () => {
        if (!dateFrom.value) return setDateRange(fromDay, toDay);
        const day = toDayNumber(dateFrom.value);
        setDateRange(day, Math.max(day, toDay));
    });
    dateTo.addEventListener("change", () => {
        if (!dateTo.value) return setDateRange(fromDay, toDay);
        const day = toDayNumber(dateTo.value);
        setDateRange(Math.min(day, fromDay), day);
    });

    /* ---- Parcels. ---- */
    const STYLE_PASSING = { color: "#c026d3", weight: 1, fillColor: "#22c55e", fillOpacity: 0.55 };
    const STYLE_OTHER = { color: "#c026d3", weight: 1, fillOpacity: 0 };
    const loaded = new Map();
    const parcels = L.featureGroup().addTo(map);

    function formatDate(iso) {
        const [year, month, day] = iso.split("-").map(Number);
        return new Date(year, month - 1, day).toLocaleDateString();
    }

    function popupFor(props, passings) {
        const box = document.createElement("div");
        const line = (text, tag) => {
            const node = document.createElement(tag || "div");
            node.textContent = text;
            box.appendChild(node);
        };
        line(props.PROP_LOC || "No address on file", "strong");
        if (props.MUN_NAME) line(props.MUN_NAME);
        line("Block " + (props.PCLBLOCK || "—") + " · Lot " + (props.PCLLOT || "—"));
        if (props.CALC_ACRE) line(Number(props.CALC_ACRE).toFixed(2) + " acres");
        const dates = passings.map((p) => p[3]).filter(Boolean).sort();
        line(!passings.length ? "Not available" : dates.length ? "Available since " + formatDate(dates[0]) : "Available");
        for (const id of passings.slice(0, 5).map((p) => p[0])) {
            const link = document.createElement("a");
            link.href = el.dataset.pasUrl.replace("{place_id}", encodeURIComponent(id));
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = "Open in PAS (" + id + ")";
            link.style.display = "block";
            box.appendChild(link);
        }
        return box;
    }

    function addFeature(feature) {
        const id = feature.properties.OBJECTID;
        if (loaded.has(id)) return;
        const passings = passingsIn(feature.geometry);
        const layer = L.geoJSON(feature, { style: isShaded(passings) ? STYLE_PASSING : STYLE_OTHER });
        layer.bindPopup(() => popupFor(feature.properties, passings));
        loaded.set(id, { layer, passings });
        parcels.addLayer(layer);
    }

    function isShaded(passings) {
        return passings.some(inDateRange);
    }

    function showParcelCount(capped) {
        const shaded = [...loaded.values()].filter((parcel) => isShaded(parcel.passings)).length;
        setStatus(
            loaded.size.toLocaleString() + " parcels loaded · " + shaded.toLocaleString() + " available in these dates" +
            (capped ? " · zoom in to load every parcel here" : "")
        );
    }

    function clearParcels() {
        parcels.clearLayers();
        loaded.clear();
    }

    let controller = null;
    let lastCapped = false;

    async function loadParcels() {
        if (controller) controller.abort();
        if (map.getZoom() < MIN_PARCEL_ZOOM) {
            clearParcels();
            setStatus("Zoom in to see parcels");
            return;
        }
        if (loaded.size > MAX_LOADED) clearParcels();

        const request = new AbortController();
        controller = request;
        const b = map.getBounds();
        setStatus("Loading parcels…");
        try {
            let capped = true;
            for (let page = 0; page < MAX_PAGES; page++) {
                const params = new URLSearchParams({
                    where: "1=1",
                    geometry: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(","),
                    geometryType: "esriGeometryEnvelope",
                    inSR: "4326",
                    spatialRel: "esriSpatialRelIntersects",
                    outFields: OUT_FIELDS,
                    outSR: "4326",
                    geometryPrecision: "6",
                    orderByFields: "OBJECTID",
                    resultOffset: String(page * PAGE_SIZE),
                    resultRecordCount: String(PAGE_SIZE),
                    f: "geojson",
                });
                const response = await fetch(el.dataset.parcelsUrl + "?" + params, { signal: request.signal });
                if (!response.ok) throw new Error("HTTP " + response.status);
                const data = await response.json();
                if (data.error) throw new Error(data.error.message || "Parcel service error");
                const features = data.features || [];
                features.forEach(addFeature);
                if (features.length < PAGE_SIZE) { capped = false; break; }
            }
            lastCapped = capped;
            showParcelCount(capped);
        } catch (error) {
            if (error.name !== "AbortError") setStatus("Couldn't load parcels from the state service. Move the map to try again.");
        }
    }

    let timer = null;
    map.on("moveend", () => {
        clearTimeout(timer);
        timer = setTimeout(loadParcels, 250);
    });

    // Opens at parcel zoom on the middle passing, so shapes show right away.
    function openOnPassings() {
        if (!points.length) return;
        const middle = [...points].sort((a, b) => a[1] - b[1])[Math.floor(points.length / 2)];
        map.setView([middle[1], middle[2]], MIN_PARCEL_ZOOM);
    }

    /* ---- Start: passings first, so parcels can be matched as they arrive. ---- */
    fetch(el.dataset.pointsUrl)
        .then((response) => (response.ok ? response.json() : Promise.reject(new Error("HTTP " + response.status))))
        .then((data) => {
            points = data.points || [];
            for (const p of points) {
                const key = cellKey(p[1], p[2]);
                if (!buckets.has(key)) buckets.set(key, []);
                buckets.get(key).push(p);
            }
            setUpDateRange();
            openOnPassings();
            loadParcels();
        })
        .catch(() => {
            setStatus("Couldn't load passings, so parcels can't be highlighted. Refresh the page to try again.");
        });
})();
