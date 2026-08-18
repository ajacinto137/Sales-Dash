// Admin Portal -- Excel user import (templates/admin/import.html).
// select -> validate -> preview -> import -> results, each a distinct
// visible state (.td-import-step-active/-done), matching the explicit
// stepper the task asked for. Validation happens the moment a file is
// selected (POST /admin/users/import/validate, multipart) -- the Import
// button stays disabled until that comes back with at least one
// importable row. Commit (POST /admin/users/import/commit) sends back
// the exact row data the preview showed; the server re-validates every
// row from scratch rather than trusting this client-held copy (see
// import_service.py).
document.addEventListener("DOMContentLoaded", function () {
    var fileInput = document.querySelector("[data-import-file-input]");
    if (!fileInput) return;

    var steps = document.querySelectorAll("[data-import-step]");
    var selectPanel = document.querySelector('[data-import-panel="select"]');
    var validatePanel = document.querySelector('[data-import-panel="validate-result"]');
    var resultsPanel = document.querySelector('[data-import-panel="results"]');
    var fileErrorEl = document.querySelector("[data-import-file-error]");
    var summaryWrap = document.querySelector("[data-import-summary-wrap]");
    var validMessageEl = document.querySelector("[data-import-valid-message]");
    var summaryEl = document.querySelector("[data-import-summary]");
    var previewBody = document.querySelector("[data-import-preview-body]");
    var commitBtn = document.querySelector("[data-import-commit]");
    var resultsSummaryEl = document.querySelector("[data-import-results-summary]");
    var resultsListEl = document.querySelector("[data-import-results-list]");

    var validatedRows = [];

    function setStep(name) {
        var order = ["select", "validate", "preview", "import", "results"];
        var targetIndex = order.indexOf(name);
        steps.forEach(function (el) {
            var stepIndex = order.indexOf(el.getAttribute("data-import-step"));
            el.classList.toggle("td-import-step-active", stepIndex === targetIndex);
            el.classList.toggle("td-import-step-done", stepIndex < targetIndex);
        });
    }

    function resetToSelect() {
        fileInput.value = "";
        validatedRows = [];
        selectPanel.style.display = "";
        validatePanel.style.display = "none";
        resultsPanel.style.display = "none";
        fileErrorEl.style.display = "none";
        summaryWrap.style.display = "none";
        commitBtn.disabled = true;
        setStep("select");
    }

    document.querySelectorAll("[data-import-reset]").forEach(function (btn) {
        btn.addEventListener("click", resetToSelect);
    });

    function summaryStat(label, value) {
        var stat = document.createElement("div");
        stat.className = "td-import-summary-stat";
        var valueEl = document.createElement("span");
        valueEl.className = "td-import-summary-value";
        valueEl.textContent = value;
        var labelEl = document.createElement("span");
        labelEl.className = "td-import-summary-label";
        labelEl.textContent = label;
        stat.appendChild(valueEl);
        stat.appendChild(labelEl);
        return stat;
    }

    function renderPreview(rows, summary) {
        previewBody.textContent = "";
        rows.forEach(function (row) {
            var tr = document.createElement("tr");

            [row.row_number, row.rep_name, row.email, row.group].forEach(function (value) {
                var td = document.createElement("td");
                td.textContent = value;
                tr.appendChild(td);
            });

            var matchTd = document.createElement("td");
            matchTd.textContent = row.matched_rep ? row.matched_rep.name : "No match";
            tr.appendChild(matchTd);

            var actionTd = document.createElement("td");
            var badge = document.createElement("span");
            badge.className = "td-pill " + (
                row.proposed_action === "Create User" ? "td-pill-active" :
                row.proposed_action === "Update Existing User" ? "td-pill-sales-rep" :
                row.proposed_action === "Needs Review" ? "td-pill-review-needed" :
                "td-pill-disabled"
            );
            badge.textContent = row.proposed_action;
            actionTd.appendChild(badge);
            if (row.errors && row.errors.length) {
                var errDiv = document.createElement("div");
                errDiv.className = "td-import-row-error";
                errDiv.textContent = row.errors.join(" ");
                actionTd.appendChild(errDiv);
            } else if (row.review_reason) {
                var reviewDiv = document.createElement("div");
                reviewDiv.className = "td-import-row-error";
                reviewDiv.textContent = row.review_reason;
                actionTd.appendChild(reviewDiv);
            }
            tr.appendChild(actionTd);

            previewBody.appendChild(tr);
        });

        summaryEl.textContent = "";
        summaryEl.appendChild(summaryStat("Total Rows", summary.total));
        summaryEl.appendChild(summaryStat("Create", summary["Create User"]));
        summaryEl.appendChild(summaryStat("Update", summary["Update Existing User"]));
        summaryEl.appendChild(summaryStat("Needs Review", summary["Needs Review"]));
        summaryEl.appendChild(summaryStat("Cannot Import", summary["Cannot Import"]));

        var importable = summary.total - summary["Cannot Import"];
        validMessageEl.textContent = importable > 0
            ? "File Valid — " + importable + " row" + (importable === 1 ? "" : "s") + " ready to import."
            : "File Valid, but no rows are ready to import — fix the errors below and re-upload.";

        commitBtn.disabled = importable <= 0;
    }

    fileInput.addEventListener("change", function () {
        if (!fileInput.files.length) return;

        validatePanel.style.display = "";
        fileErrorEl.style.display = "none";
        summaryWrap.style.display = "none";
        setStep("validate");

        var formData = new FormData();
        formData.append("file", fileInput.files[0]);

        fetch("/admin/users/import/validate", { method: "POST", body: formData })
            .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
            .then(function (result) {
                if (!result.ok || !result.data.ok) {
                    fileErrorEl.textContent = "File cannot be imported — " + ((result.data && result.data.error) || "unknown error.");
                    fileErrorEl.style.display = "block";
                    return;
                }
                validatedRows = result.data.rows;
                renderPreview(result.data.rows, result.data.summary);
                summaryWrap.style.display = "";
                setStep("preview");
            })
            .catch(function () {
                fileErrorEl.textContent = "File cannot be imported — network error, please try again.";
                fileErrorEl.style.display = "block";
            });
    });

    commitBtn.addEventListener("click", function () {
        commitBtn.disabled = true;
        setStep("import");

        fetch("/admin/users/import/commit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rows: validatedRows }),
        })
            .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
            .then(function (result) {
                if (!result.ok || !result.data.ok) {
                    fileErrorEl.textContent = (result.data && result.data.error) || "Import failed.";
                    fileErrorEl.style.display = "block";
                    commitBtn.disabled = false;
                    return;
                }
                renderResults(result.data);
            })
            .catch(function () {
                fileErrorEl.textContent = "Import failed — network error, please try again.";
                fileErrorEl.style.display = "block";
                commitBtn.disabled = false;
            });
    });

    function renderResults(data) {
        validatePanel.style.display = "none";
        resultsPanel.style.display = "";
        setStep("results");

        resultsSummaryEl.textContent = "";
        resultsSummaryEl.appendChild(summaryStat("Added", data.added));
        resultsSummaryEl.appendChild(summaryStat("Updated", data.updated));
        resultsSummaryEl.appendChild(summaryStat("Needs Review", data.needs_review));
        resultsSummaryEl.appendChild(summaryStat("Failed", data.failed.length));

        resultsListEl.textContent = "";
        if (data.failed.length) {
            var header = document.createElement("div");
            header.style.fontWeight = "700";
            header.style.fontSize = "12.5px";
            header.textContent = "Failed rows:";
            resultsListEl.appendChild(header);

            data.failed.forEach(function (failure) {
                var item = document.createElement("div");
                item.className = "td-import-result-item";
                var strong = document.createElement("strong");
                strong.textContent = (failure.rep_name || "(no name)") + " — " + (failure.email || "(no email)");
                item.appendChild(strong);
                item.appendChild(document.createTextNode(" (row " + failure.row_number + ") — " + failure.reason));
                resultsListEl.appendChild(item);
            });

            var downloadBtn = document.createElement("button");
            downloadBtn.type = "button";
            downloadBtn.className = "td-btn";
            downloadBtn.style.marginTop = "10px";
            downloadBtn.textContent = "Download Failed Rows (CSV)";
            downloadBtn.addEventListener("click", function () {
                downloadFailedRowsCsv(data.failed);
            });
            resultsListEl.appendChild(downloadBtn);
        }
    }

    function downloadFailedRowsCsv(failed) {
        var lines = ["Row,Rep Name,Email,Reason"];
        failed.forEach(function (failure) {
            var cells = [failure.row_number, failure.rep_name, failure.email, failure.reason].map(function (value) {
                var text = String(value == null ? "" : value).replace(/"/g, '""');
                return '"' + text + '"';
            });
            lines.push(cells.join(","));
        });
        var blob = new Blob([lines.join("\n")], { type: "text/csv" });
        var url = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = url;
        link.download = "failed_import_rows.csv";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }
});
