// Global search bar (top nav, templates/_topnav.html) -- reps and
// customer accounts, debounced fetch to /search (see app.py), which
// itself deliberately skips the full data reload every other route now
// does on every request (see that route's docstring), so this stays
// fast even while typing. A no-op on any page without [data-global-search]
// (there are none right now, but this stays defensive the way every
// other feature-scoped script in this app does).
document.addEventListener("DOMContentLoaded", function () {
    var container = document.querySelector("[data-global-search]");
    if (!container) return;

    var input = container.querySelector("[data-global-search-input]");
    var resultsPanel = container.querySelector("[data-global-search-results]");
    var debounceTimer = null;
    var activeController = null;

    function closeResults() {
        resultsPanel.style.display = "none";
        resultsPanel.textContent = "";
    }

    function renderMessage(message) {
        resultsPanel.textContent = "";
        var empty = document.createElement("div");
        empty.className = "td-global-search-empty";
        empty.textContent = message;
        resultsPanel.appendChild(empty);
        resultsPanel.style.display = "block";
    }

    function addGroupLabel(text) {
        var label = document.createElement("div");
        label.className = "td-global-search-group-label";
        label.textContent = text;
        resultsPanel.appendChild(label);
    }

    // type is "rep" or "account" -- drives both the colored badge text
    // and its color (.td-global-search-type-rep/-account in
    // team_dashboard.css), which is what makes it obvious at a glance
    // which kind of result each row is.
    function buildItem(type, title, subtitle, url, external) {
        var item = document.createElement("a");
        item.className = "td-global-search-item";
        item.href = url;
        if (external) {
            item.target = "_blank";
            item.rel = "noopener noreferrer";
        }

        var badge = document.createElement("span");
        badge.className = "td-global-search-type-badge td-global-search-type-" + type;
        badge.textContent = type === "rep" ? "Rep" : "Account";
        item.appendChild(badge);

        var main = document.createElement("div");
        main.className = "td-global-search-item-main";

        var titleEl = document.createElement("span");
        titleEl.className = "td-global-search-item-title";
        titleEl.textContent = title;
        main.appendChild(titleEl);

        if (subtitle) {
            var subEl = document.createElement("span");
            subEl.className = "td-global-search-item-sub";
            subEl.textContent = subtitle;
            main.appendChild(subEl);
        }

        item.appendChild(main);
        return item;
    }

    function renderResults(data) {
        resultsPanel.textContent = "";
        var reps = data.reps || [];
        var accounts = data.accounts || [];

        if (!reps.length && !accounts.length) {
            renderMessage("No reps or accounts match.");
            return;
        }

        if (reps.length) {
            addGroupLabel("Reps");
            reps.forEach(function (rep) {
                resultsPanel.appendChild(buildItem("rep", rep.name, "Individual Sales Profile", rep.url, false));
            });
        }

        if (accounts.length) {
            addGroupLabel("Accounts");
            accounts.forEach(function (account) {
                var title = (account.first_name + " " + account.last_name).trim() || "Unnamed account";
                var subParts = [];
                if (account.address) subParts.push(account.address);
                subParts.push("Sold by " + account.sales_rep);
                var item = buildItem("account", title, subParts.join(" · "), account.url, account.external);
                resultsPanel.appendChild(item);
            });
        }

        resultsPanel.style.display = "block";
    }

    function runSearch(query) {
        if (activeController) {
            activeController.abort();
        }
        activeController = new AbortController();

        fetch("/search?q=" + encodeURIComponent(query), { signal: activeController.signal })
            .then(function (res) {
                return res.json();
            })
            .then(function (data) {
                renderResults(data);
            })
            .catch(function (err) {
                if (err.name === "AbortError") return;
                renderMessage("Search failed -- please try again.");
            });
    }

    input.addEventListener("input", function () {
        var query = input.value.trim();
        clearTimeout(debounceTimer);
        if (!query) {
            closeResults();
            return;
        }
        // Short debounce -- this is a lightweight in-memory lookup (see
        // /search's docstring), not a full page reload, so it can afford
        // to feel closer to live-as-you-type than the Individual Rep
        // Leaderboard's own 450ms rep-search debounce.
        debounceTimer = setTimeout(function () {
            runSearch(query);
        }, 150);
    });

    input.addEventListener("focus", function () {
        if (input.value.trim() && resultsPanel.childNodes.length) {
            resultsPanel.style.display = "block";
        }
    });

    input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            closeResults();
            input.blur();
        }
    });

    document.addEventListener("click", function (e) {
        if (!container.contains(e.target)) {
            closeResults();
        }
    });
});
