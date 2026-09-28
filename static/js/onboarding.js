/* Onboarding: choose between a private workspace and asking to join a team.
 *
 * Buttons are wired with addEventListener rather than inline onclick because
 * the Content-Security-Policy forbids inline scripts.
 */

function setStatus(message, isError = false) {
    const el = document.getElementById("onboarding-status");
    if (!el) return;
    el.innerHTML = message
        ? `<span class="flash ${isError ? "flash--error" : "flash--success"} flash--inline">${message}</span>`
        : "";
}

async function createOwnTeam() {
    const btn = document.getElementById("create-own-team");
    if (btn) btn.disabled = true;
    setStatus("Creating your workspace…");
    try {
        const resp = await fetch("/api/onboarding/create-team", { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
            window.location.href = data.redirect || "/dashboard";
            return;
        }
        setStatus(escapeHtml(data.error || "Could not create your workspace."), true);
    } catch (e) {
        setStatus(escapeHtml(e.message), true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function requestAccess() {
    const emailEl = document.getElementById("owner-email");
    const descEl = document.getElementById("join-description");
    const btn = document.getElementById("request-access");

    const owner_email = (emailEl?.value || "").trim();
    const description = (descEl?.value || "").trim();

    if (!owner_email || !owner_email.includes("@")) {
        setStatus("Enter the team owner's email address.", true);
        return;
    }
    if (description.length < 3) {
        setStatus("Add a short note so the owner knows who you are.", true);
        return;
    }

    if (btn) btn.disabled = true;
    setStatus("Sending your request…");
    try {
        const resp = await fetch("/api/onboarding/request-access", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ owner_email, description }),
        });
        const data = await resp.json();
        if (data.ok) {
            // The server answers identically whether or not the address
            // matched a team, so this wording must not imply a match.
            setStatus(
                "✓ Request sent. If that address belongs to a team owner, they'll " +
                "see your request and can approve or decline it."
            );
            if (descEl) descEl.value = "";
            updateCount();
        } else if (resp.status === 429) {
            const wait = data.retry_after_seconds
                ? ` Try again in about ${Math.ceil(data.retry_after_seconds / 60)} minute(s).`
                : ""
            setStatus(`✗ ${escapeHtml(data.error || "Too many requests.")}${wait}`, true);
        } else {
            setStatus(`✗ ${escapeHtml(data.error || "Could not send your request.")}`, true);
        }
    } catch (e) {
        setStatus(`✗ ${escapeHtml(e.message)}`, true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

function updateCount() {
    const descEl = document.getElementById("join-description");
    const countEl = document.getElementById("description-count");
    if (descEl && countEl) countEl.textContent = String(descEl.value.length);
}

document.addEventListener("DOMContentLoaded", () => {
    const createBtn = document.getElementById("create-own-team");
    if (createBtn) createBtn.addEventListener("click", createOwnTeam);

    const requestBtn = document.getElementById("request-access");
    if (requestBtn) requestBtn.addEventListener("click", requestAccess);

    const descEl = document.getElementById("join-description");
    if (descEl) descEl.addEventListener("input", updateCount);
    updateCount();
});
