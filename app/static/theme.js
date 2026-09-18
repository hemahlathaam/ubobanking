/**
 * RUBO × COB — Theme toggle
 * Persists the choice in localStorage. Defaults to light.
 */

(function () {
    const STORAGE_KEY = "rubo-theme";

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);

        const icon = document.querySelector(".theme-toggle-icon");
        const text = document.querySelector(".theme-toggle-text");

        if (icon && text) {
            if (theme === "dark") {
                icon.textContent = "☀";
                text.textContent = "Light mode";
            } else {
                icon.textContent = "🌙";
                text.textContent = "Dark mode";
            }
        }
    }

    function getStoredTheme() {
        try {
            return localStorage.getItem(STORAGE_KEY) || "light";
        } catch (e) {
            return "light";
        }
    }

    function setStoredTheme(theme) {
        try {
            localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) {
            // localStorage unavailable — silently ignore
        }
    }

    // Apply on page load, as early as possible
    applyTheme(getStoredTheme());

    // Called by the button in base.html
    window.toggleTheme = function () {
        const current = document.documentElement.getAttribute("data-theme") || "light";
        const next = current === "dark" ? "light" : "dark";
        applyTheme(next);
        setStoredTheme(next);
    };

    // Re-apply once DOM is ready (in case the button renders after this script)
    document.addEventListener("DOMContentLoaded", function () {
        applyTheme(getStoredTheme());
    });
})();
