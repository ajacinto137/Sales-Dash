document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[action*='/refresh']").forEach(function (form) {
        form.addEventListener("submit", function () {
            var button = form.querySelector("button[type='submit']");
            if (button) {
                button.disabled = true;
                button.dataset.originalText = button.textContent;
                button.textContent = "Refreshing…";
            }
        });
    });
});
