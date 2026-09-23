/* ================================================================
   Marketplace Dashboard — JavaScript
   ================================================================

   Pure vanilla JS — no frameworks, no build tools.
   Handles:
   - Dashboard: load & display listings with filtering/pagination.
   - Admin: OAuth flow, manual sync, plugin list.
   - Flash messages: auto-dismiss after 4 seconds.

   ================================================================== */

/* --- Theme toggle (Dark / Light / Auto) --- */
(function () {
    const toggleBtn = document.getElementById("theme-toggle");
    const sunIcon = document.getElementById("theme-icon-sun");
    const moonIcon = document.getElementById("theme-icon-moon");

    function getPreferredTheme() {
        const stored = localStorage.getItem("theme");
        if (stored) return stored;
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function applyTheme(theme) {
        if (theme === "dark") {
            document.documentElement.setAttribute("data-theme", "dark");
            if (sunIcon) sunIcon.style.display = "none";
            if (moonIcon) moonIcon.style.display = "block";
        } else {
            document.documentElement.setAttribute("data-theme", "light");
            if (sunIcon) sunIcon.style.display = "block";
            if (moonIcon) moonIcon.style.display = "none";
        }
    }

    const currentTheme = getPreferredTheme();
    applyTheme(currentTheme);

    if (toggleBtn) {
        toggleBtn.addEventListener("click", function () {
            const isDark = document.documentElement.getAttribute("data-theme") === "dark";
            const newTheme = isDark ? "light" : "dark";
            localStorage.setItem("theme", newTheme);
            applyTheme(newTheme);
        });
    }

    // Listen for system theme changes if user hasn't explicitly overridden
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
        if (!localStorage.getItem("theme")) {
            applyTheme(e.matches ? "dark" : "light");
        }
    });
})();

/* --- Flash message auto-dismiss (shared by all pages) --- */
(function () {
    const flashes = document.querySelectorAll(".flash");
    flashes.forEach(function (el) {
        setTimeout(function () {
            el.style.transition = "opacity 0.4s";
            el.style.opacity = "0";
            setTimeout(function () { el.remove(); }, 400);
        }, 4000);
    });
})();

/* --- Sign out (button lives in the base nav, so this runs on every page) --- */
(function () {
    const btn = document.getElementById("signout-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
        btn.disabled = true;
        btn.textContent = "Signing out…";
        // The response deletes the session server-side and the auth
        // cookie; then we hop to the login page.
        fetch("/api/auth/logout", { method: "POST" })
            .catch(function () { /* network errors are fine — the cookie dies with the response */ })
            .finally(function () {
                location.href = "/login?logged_out=true";
            });
    });
})();

/* --- Shared helpers (used by dashboard.js and admin.js) --- */

/* Escape a string for safe insertion into HTML (prevents XSS). */
function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = String(str);
    return div.innerHTML;
}
