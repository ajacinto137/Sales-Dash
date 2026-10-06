// Auto-update (dashboard.html header, [data-auto-refresh]) -- reloads the
// page on a timer so the Team Leaderboard stays current without someone
// getting up to click "Refresh Data" (_topnav.html) or hitting the
// browser's own refresh. No new data-fetching needed for this: every
// full-page GET already re-queries both source databases live on every
// request (see app.py's ensure_data_loaded()), so a plain
// window.location.reload() IS a real data refresh here, not just a
// cache-buster. A no-op on any page without [data-auto-refresh] (only
// dashboard.html has it right now), same defensive pattern as every
// other feature-scoped script in this app (search.js, attention.js, ...).
//
// On/off + interval is a per-browser preference (localStorage), not a
// server setting -- there's no "everyone's dashboard reloads on the same
// clock" requirement, and this way it survives navigating away and back.
// Off by default: an unattended wall-mounted screen is a real use case
// for this, but so is someone actively working the Needs Attention list,
// and auto-reloading out from under that without being asked would be
// worse than just leaving the manual Refresh Data button as-is.
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

    function readStoredBool(key) {
        try {
            return window.localStorage.getItem(key) === "1";
        } catch (e) {
            return false;
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
    toggle.checked = readStoredBool(STORAGE_ENABLED);

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
