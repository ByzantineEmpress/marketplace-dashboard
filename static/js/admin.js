/* ================================================================
   Admin page logic
   Handles the OAuth connect flow, manual syncs, and the plugin
   list. Lives in a static file (not inline) so the strict
   Content-Security-Policy (script-src 'self') still allows it.

   Buttons are wired up here with addEventListener instead of
   inline onclick attributes — inline handlers are also blocked
   by CSP. Shared helpers (escapeHtml) come from app.js.
   ================================================================ */

async function startOAuth(platform) {
    const log = document.getElementById("sync-log");
    log.innerHTML = `<p>Redirecting to ${platform} for authorisation...</p>`;

    try {
        const resp = await fetch("/api/accounts/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform }),
        });
        const data = await resp.json();

        if (data.auth_url) {
            window.location.href = data.auth_url;
        } else {
            log.innerHTML = `<p class="flash flash--error">Error: ${data.error || "Unknown error"}</p>`;
        }
    } catch (e) {
        log.innerHTML = `<p class="flash flash--error">Connection failed: ${e.message}</p>`;
    }
}

async function syncPlatform(platform) {
    const log = document.getElementById("sync-log");
    log.innerHTML = `<p>Syncing ${platform} listings...</p>`;

    try {
        const resp = await fetch("/api/accounts/sync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform }),
        });
        const data = await resp.json();

        if (data.ok) {
            const r = data.result || {};
            log.innerHTML = `<p class="flash flash--success">✓ Synced ${data.platform}: fetched ${r.listings_fetched ?? 0}, added ${r.listings_added ?? 0}, updated ${r.listings_updated ?? 0}</p>`;
        } else {
            const errors = data.errors || [data.error || "Unknown error"];
            log.innerHTML = `<p class="flash flash--error">✗ Sync failed: ${errors.join("; ")}</p>`;
        }
    } catch (e) {
        log.innerHTML = `<p class="flash flash--error">Sync error: ${e.message}</p>`;
    }
}

// ---------- Google Sign-In settings ----------

async function loadAllSettings() {
    const redirectUri = document.getElementById("google-redirect-uri");
    const ebayRedirect = document.getElementById("ebay-redirect-uri");
    const etsyRedirect = document.getElementById("etsy-redirect-uri");
    const poshmarkRedirect = document.getElementById("poshmark-redirect-uri");
    const amazonRedirect = document.getElementById("amazon-redirect-uri");
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) return;
        const data = await resp.json();
        const s = data.settings || {};
        if (redirectUri) redirectUri.value = data.google_redirect_uri || "";
        if (ebayRedirect) ebayRedirect.value = data.ebay_redirect_uri || "";
        if (etsyRedirect) etsyRedirect.value = data.etsy_redirect_uri || "";
        if (poshmarkRedirect) poshmarkRedirect.value = data.poshmark_redirect_uri || "";
        if (amazonRedirect) amazonRedirect.value = data.amazon_redirect_uri || "";

        const setIf = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.value = value || "";
        };
        setIf("google-client-id", s.GOOGLE_CLIENT_ID);
        setIf("google-client-secret", s.GOOGLE_CLIENT_SECRET);
        setIf("google-allowed-emails", s.GOOGLE_ALLOWED_EMAILS);
        const devModeCheckbox = document.getElementById("google-dev-mode");
        if (devModeCheckbox) {
            devModeCheckbox.checked = Boolean(s.GOOGLE_DEV_MODE);
        }

        setIf("ebay-client-id", s.EBAY_CLIENT_ID);
        setIf("ebay-client-secret", s.EBAY_CLIENT_SECRET);
        setIf("etsy-api-key", s.ETSY_API_KEY);
        setIf("etsy-api-secret", s.ETSY_API_SECRET);

        setIf("poshmark-username", s.POSHMARK_USERNAME);
        setIf("poshmark-api-key", s.POSHMARK_API_KEY);

        setIf("amazon-seller-id", s.AMAZON_SELLER_ID);
        setIf("amazon-client-id", s.AMAZON_CLIENT_ID);
        setIf("amazon-client-secret", s.AMAZON_CLIENT_SECRET);
        setIf("amazon-refresh-token", s.AMAZON_REFRESH_TOKEN);
        setIf("amazon-marketplace-id", s.AMAZON_MARKETPLACE_ID || "ATVPDKIKX0DER");
    } catch (e) {
        /* leave the form at its defaults */
    }
}

async function saveGoogleSettings() {
    const status = document.getElementById("google-settings-status");
    const devModeCheckbox = document.getElementById("google-dev-mode");
    const body = {
        GOOGLE_CLIENT_ID: (document.getElementById("google-client-id")?.value || "").trim(),
        GOOGLE_CLIENT_SECRET: (document.getElementById("google-client-secret")?.value || "").trim(),
        GOOGLE_ALLOWED_EMAILS: (document.getElementById("google-allowed-emails")?.value || "").trim(),
        GOOGLE_DEV_MODE: devModeCheckbox ? devModeCheckbox.checked : true,
    };
    try {
        const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            status.innerHTML =
                '<span class="flash flash--success flash--inline">✓ Saved — sign-in settings updated.</span>';
        } else {
            status.innerHTML =
                `<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`;
        }
    } catch (e) {
        status.innerHTML =
            `<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`;
    }
}

async function saveMarketplaceSettings() {
    const status = document.getElementById("marketplace-settings-status");
    const body = {
        EBAY_CLIENT_ID: (document.getElementById("ebay-client-id")?.value || "").trim(),
        EBAY_CLIENT_SECRET: (document.getElementById("ebay-client-secret")?.value || "").trim(),
        ETSY_API_KEY: (document.getElementById("etsy-api-key")?.value || "").trim(),
        ETSY_API_SECRET: (document.getElementById("etsy-api-secret")?.value || "").trim(),
        POSHMARK_USERNAME: (document.getElementById("poshmark-username")?.value || "").trim(),
        POSHMARK_API_KEY: (document.getElementById("poshmark-api-key")?.value || "").trim(),
        AMAZON_SELLER_ID: (document.getElementById("amazon-seller-id")?.value || "").trim(),
        AMAZON_CLIENT_ID: (document.getElementById("amazon-client-id")?.value || "").trim(),
        AMAZON_CLIENT_SECRET: (document.getElementById("amazon-client-secret")?.value || "").trim(),
        AMAZON_REFRESH_TOKEN: (document.getElementById("amazon-refresh-token")?.value || "").trim(),
        AMAZON_MARKETPLACE_ID: (document.getElementById("amazon-marketplace-id")?.value || "").trim(),
    };
    try {
        const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            status.innerHTML =
                '<span class="flash flash--success flash--inline">✓ Marketplace credentials saved.</span>';
        } else {
            status.innerHTML =
                `<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`;
        }
    } catch (e) {
        status.innerHTML =
            `<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`;
    }
}

// Load plugin list on page load
async function loadPlugins() {
    const list = document.getElementById("plugin-list");
    try {
        const resp = await fetch("/api/plugins");
        const plugins = await resp.json();

        if (plugins.length === 0) {
            list.innerHTML = "<p>No plugins loaded. Drop <code>.py</code> files into the <code>plugins/</code> directory.</p>";
        } else {
            list.innerHTML = plugins.map(p => `
                <div class="plugin-card">
                    <strong>${escapeHtml(p.name)}</strong>
                    <span class="plugin-version">v${escapeHtml(p.version)}</span>
                    <p>${escapeHtml(p.description || "")}</p>
                </div>
            `).join("");
        }
    } catch (e) {
        list.innerHTML = "<p>Could not load plugin list.</p>";
    }
}

loadPlugins();

// ---------- Teams (shared inventory) ----------

// escapeHtml (from app.js) escapes < > & but not quotes — attribute
// values need one extra pass so a name like O"Brien can't break the tag.
function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, "&quot;");
}

function showTeamsStatus(message, isError = false) {
    const el = document.getElementById("teams-status");
    if (el) {
        el.innerHTML = `<span class="flash ${isError ? "flash--error" : "flash--success"} flash--inline">${message}</span>`;
    }
}

async function loadTeams() {
    const list = document.getElementById("teams-list");
    if (!list) return;
    try {
        const resp = await fetch("/api/teams");
        const teams = await resp.json();
        if (!Array.isArray(teams) || teams.length === 0) {
            list.innerHTML = "<p>No teams yet — create one below.</p>";
            return;
        }
        list.innerHTML = teams.map(team => `
            <div class="team-card">
                <div class="team-header">
                    <strong>${escapeHtml(team.name)}</strong>
                    <span class="team-role-badge team-role-badge--${escapeHtml(team.role)}">${escapeHtml(team.role)}</span>
                </div>
                <ul class="team-members">
                    ${team.members.map(m => `
                        <li>
                            <span class="member-name">${escapeHtml(m.name)} <span class="member-email">${escapeHtml(m.email)}</span></span>
                            <span class="team-actions-right">
                                <span class="team-role-badge team-role-badge--${escapeHtml(m.role)}">${escapeHtml(m.role)}</span>
                                <button class="btn btn--sm btn--danger team-remove-member"
                                        data-team="${team.id}" data-user="${m.id}"
                                        data-name="${escapeAttr(m.name)}">Remove</button>
                            </span>
                        </li>
                    `).join("")}
                </ul>
                <div class="team-add-member">
                    <input type="email" class="member-email-input" data-team="${team.id}" placeholder="Add a member by email…">
                    <button class="btn btn--sm team-add-btn" data-team="${team.id}">Add Member</button>
                </div>
            </div>
        `).join("");
        // Wire the per-team buttons (the list is small — direct wiring
        // keeps the code easier to follow than event delegation here).
        list.querySelectorAll(".team-add-btn").forEach(btn => {
            btn.addEventListener("click", () => addTeamMember(btn.dataset.team));
        });
        list.querySelectorAll(".team-remove-member").forEach(btn => {
            btn.addEventListener("click", () => removeTeamMember(btn.dataset.team, btn.dataset.user, btn.dataset.name));
        });
    } catch (e) {
        list.innerHTML = `<p class="flash flash--error">Could not load teams: ${escapeHtml(e.message)}</p>`;
    }
}

async function createTeam() {
    const name = document.getElementById("new-team-name").value.trim();
    if (!name) {
        showTeamsStatus("Give the team a name first.", true);
        return;
    }
    try {
        const resp = await fetch("/api/teams", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });
        const data = await resp.json();
        if (data.ok) {
            document.getElementById("new-team-name").value = "";
            showTeamsStatus(`✓ Team "${escapeHtml(name)}" created.`);
            loadTeams();
        } else {
            showTeamsStatus(`✗ ${escapeHtml(data.error || "Could not create team")}`, true);
        }
    } catch (e) {
        showTeamsStatus(`✗ ${escapeHtml(e.message)}`, true);
    }
}

async function addTeamMember(teamId) {
    const input = document.querySelector(`.member-email-input[data-team="${teamId}"]`);
    const email = (input.value || "").trim();
    if (!email) return;
    try {
        const resp = await fetch(`/api/teams/${teamId}/members`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email }),
        });
        const data = await resp.json();
        if (data.ok) {
            input.value = "";
            showTeamsStatus(`✓ ${escapeHtml(email)} added.`);
            loadTeams();
        } else {
            showTeamsStatus(`✗ ${escapeHtml(data.error || "Could not add member")}`, true);
        }
    } catch (e) {
        showTeamsStatus(`✗ ${escapeHtml(e.message)}`, true);
    }
}

async function removeTeamMember(teamId, userId, name) {
    try {
        const resp = await fetch(`/api/teams/${teamId}/members/${userId}`, { method: "DELETE" });
        const data = await resp.json();
        if (data.ok) {
            showTeamsStatus(`✓ ${escapeHtml(name)} removed.`);
            loadTeams();
        } else {
            showTeamsStatus(`✗ ${escapeHtml(data.error || "Could not remove member")}`, true);
        }
    } catch (e) {
        showTeamsStatus(`✗ ${escapeHtml(e.message)}`, true);
    }
}

// Wire up the buttons (replaces the inline onclick attributes,
// which the Content-Security-Policy would block).
document.addEventListener("DOMContentLoaded", () => {
    const wire = (id, fn) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("click", fn);
    };
    wire("connect-ebay", () => startOAuth("ebay"));
    wire("connect-etsy", () => startOAuth("etsy"));
    wire("connect-poshmark", () => startOAuth("poshmark"));
    wire("connect-amazon", () => startOAuth("amazon"));
    wire("sync-ebay", () => syncPlatform("ebay"));
    wire("sync-etsy", () => syncPlatform("etsy"));
    wire("sync-poshmark", () => syncPlatform("poshmark"));
    wire("sync-amazon", () => syncPlatform("amazon"));
    wire("save-google-settings", saveGoogleSettings);
    wire("save-marketplace-settings", saveMarketplaceSettings);
    wire("create-team", createTeam);
    loadAllSettings();
    loadTeams();
});
