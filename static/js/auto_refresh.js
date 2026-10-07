// Auto-update (dashboard.html header, [data-auto-refresh]) -- reloads the
// page on a timer so the Team Leaderboard stays current without someone
// getting up to click "Refresh Data" (_topnav.html) or hitting the
// browser's own refresh. A plain window.location.reload() is enough:
// the server refreshes its data from both source databases on its own
// background timer (app.py's ensure_data_loaded(), every 60s), and every
// page load serves the latest copy. A no-op on any page without
// [data-auto-refresh] (only dashboard.html has it right now), same
// defensive pattern as every other feature-scoped script in this app
// (search.js, attention.js, ...).
//
// On/off + interval is a per-browser preference (localStorage), not a
// server setting -- there's no "everyone's dashboard reloads on the same
// clock" requirement, and this way it survives navigating away and back.
// On by default again as of 2026-10-07. It was switched off on
// 2026-10-06 after a 504 outage, back when every page load did its own
// live PlanetWeb + KPI query behind one lock on a single gunicorn worker,
// so reloading tabs piled up database work. Page loads no longer touch
// the databases at all, so a reload costs the server almost nothing no
// matter how many tabs are doing it.
document.addEventListener("DOMContentLoaded", function () {
    var container = document.querySelector("[data-auto-refresh]");
    if (!container) return;

    var toggle = container.querySelector("[data-auto-refresh-toggle]");
    var intervalSelect = container.querySelector("[data-auto-refresh-interval]");
    var status = container.querySelector("[data-auto-refresh-status]");
    if (!toggle || !intervalSelect) return;

    var STORAGE_ENABLED = "td-auto-refresh-enabled";
    var STORAGE_INTERVAL = "td-auto-refresh-interval-seconds";
    var DEFAULT_INTERVAL = 60;

    // Tri-state: "1"/"0" is an explicit saved choice; nothing saved yet
    // (null, first-ever visit, or storage blocked) falls back to
    // `defaultValue` instead of always reading as off.
    function readStoredBool(key, defaultValue) {
        try {
            var raw = window.localStorage.getItem(key);
            if (raw === "1") return true;
            if (raw === "0") return false;
            return defaultValue;
        } catch (e) {
            return defaultValue;
        }
    }

    function readStoredInterval() {
        try {
            var raw = parseInt(window.localStorage.getItem(STORAGE_INTERVAL), 10);
            return raw ? raw : DEFAULT_INTERVAL;
        } catch (e) {
            return DEFAULT_INTERVAL;
        }
    }

    function store(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (e) {
            // Private browsing / blocked storage -- the toggle still works
            // for this page view, it just won't be remembered next visit.
        }
    }

    var intervalSeconds = readStoredInterval();
    if (intervalSelect.querySelector('option[value="' + intervalSeconds + '"]')) {
        intervalSelect.value = String(intervalSeconds);
    } else {
        intervalSeconds = DEFAULT_INTERVAL;
        intervalSelect.value = String(DEFAULT_INTERVAL);
    }
    toggle.checked = readStoredBool(STORAGE_ENABLED, true);

    var timer = null;
    var secondsLeft = intervalSeconds;

    // Never yank the page out from under someone who's mid-interaction:
    // typing in a filter/search box or a date field, or has a Needs
    // Attention classify/note panel open (static/js/attention.js;
    // unsaved text in there would just vanish on reload). A skipped tick
    // just waits for the next one instead of reloading immediately after
    // -- if someone's actively working the page, right now almost always
    // isn't a good time either.
    function shouldSkipTick() {
        var active = document.activeElement;
        if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) {
            return true;
        }
        var openPanels = container.ownerDocument.querySelectorAll("[data-attention-panel]");
        for (var i = 0; i < openPanels.length; i++) {
            if (openPanels[i].style.display !== "none") {
                return true;
            }
        }
        return false;
    }

    function setStatus(text) {
        if (status) status.textContent = text;
    }

    function tick() {
        secondsLeft -= 1;
        if (secondsLeft > 0) {
            setStatus("next in " + secondsLeft + "s");
            return;
        }
        if (document.hidden || shouldSkipTick()) {
            // Try again next second rather than losing a whole interval --
            // a background tab or a mid-edit moment is usually brief.
            secondsLeft = 1;
            setStatus("waiting…");
            return;
        }
        setStatus("updating…");
        window.location.reload();
    }

    function start() {
        stop();
        secondsLeft = intervalSeconds;
        setStatus("next in " + secondsLeft + "s");
        timer = window.setInterval(tick, 1000);
    }

    function stop() {
        if (timer) {
            window.clearInterval(timer);
            timer = null;
        }
        setStatus("");
    }

    if (toggle.checked) start();

    toggle.addEventListener("change", function () {
        store(STORAGE_ENABLED, toggle.checked ? "1" : "0");
        if (toggle.checked) {
            start();
        } else {
            stop();
        }
    });

    intervalSelect.addEventListener("change", function () {
        intervalSeconds = parseInt(intervalSelect.value, 10) || DEFAULT_INTERVAL;
        store(STORAGE_INTERVAL, String(intervalSeconds));
        if (toggle.checked) start();
    });
});
