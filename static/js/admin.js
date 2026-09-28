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
    log.innerHTML = `<p>Redirecting to ${escapeHtml(platform)} for authorisation...</p>`;

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
            log.innerHTML = `<p class="flash flash--error">Error: ${escapeHtml(data.error || "Unknown error")}</p>`;
        }
    } catch (e) {
        log.innerHTML = `<p class="flash flash--error">Connection failed: ${escapeHtml(e.message)}</p>`;
    }
}

async function syncPlatform(platform) {
    const log = document.getElementById("sync-log");
    log.innerHTML = `<p>Syncing ${escapeHtml(platform)} listings...</p>`;

    try {
        const resp = await fetch("/api/accounts/sync", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ platform }),
        });
        const data = await resp.json();

        if (data.ok) {
            const r = data.result || {};
            log.innerHTML = `<p class="flash flash--success">✓ Synced ${escapeHtml(data.platform || platform)}: fetched ${Number(r.listings_fetched ?? 0)}, added ${Number(r.listings_added ?? 0)}, updated ${Number(r.listings_updated ?? 0)}</p>`;
        } else {
            const errors = (data.errors || [data.error || "Unknown error"]).map(err => escapeHtml(String(err)));
            log.innerHTML = `<p class="flash flash--error">✗ Sync failed: ${errors.join("; ")}</p>`;
        }
    } catch (e) {
        log.innerHTML = `<p class="flash flash--error">Sync error: ${escapeHtml(e.message)}</p>`;
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

        setIf("default-currency", s.DEFAULT_CURRENCY || "CAD");
        setIf("default-currency-symbol", s.DEFAULT_CURRENCY_SYMBOL || "$");

        setIf("smtp-host", s.SMTP_HOST);
        setIf("smtp-port", s.SMTP_PORT || "587");
        setIf("smtp-from", s.SMTP_FROM);
        setIf("smtp-user", s.SMTP_USER);
        setIf("smtp-password", s.SMTP_PASSWORD);
        const smtpTls = document.getElementById("smtp-use-tls");
        if (smtpTls) {
            smtpTls.checked = s.SMTP_USE_TLS !== false;
        }

        // Outbound email transport + provider API
        setIf("mail-backend", s.MAIL_BACKEND);
        setIf("mail-provider", s.MAIL_PROVIDER || "resend");
        setIf("mail-api-url", s.MAIL_API_URL);
        setIf("mail-api-key", s.MAIL_API_KEY);
        setIf("mail-from", s.MAIL_FROM);
        // Upload storage
        setIf("upload-backend", s.UPLOAD_BACKEND || "local");
        setIf("s3-bucket", s.S3_BUCKET);
        setIf("s3-region", s.S3_REGION);
        setIf("s3-access-key-id", s.S3_ACCESS_KEY_ID);
        setIf("s3-secret-access-key", s.S3_SECRET_ACCESS_KEY);
        setIf("s3-public-base-url", s.S3_PUBLIC_BASE_URL);
        setIf("s3-endpoint-url", s.S3_ENDPOINT_URL);
    } catch (e) {
        /* leave the form at its defaults */
    }
}

async function saveUploadSettings() {
    const status = document.getElementById("upload-settings-status");
    const setStatus = (html) => { if (status) status.innerHTML = html; };
    const backend = (document.getElementById("upload-backend")?.value || "local").trim();
    const bucket = (document.getElementById("s3-bucket")?.value || "").trim();
    if (backend === "s3" && !bucket) {
        setStatus('<span class="flash flash--error flash--inline">✗ Enter an S3 bucket name first.</span>');
        return;
    }
    const body = {
        UPLOAD_BACKEND: backend,
        S3_BUCKET: bucket,
        S3_REGION: (document.getElementById("s3-region")?.value || "").trim(),
        S3_ACCESS_KEY_ID: (document.getElementById("s3-access-key-id")?.value || "").trim(),
        S3_SECRET_ACCESS_KEY: (document.getElementById("s3-secret-access-key")?.value || "").trim(),
        S3_PUBLIC_BASE_URL: (document.getElementById("s3-public-base-url")?.value || "").trim(),
        S3_ENDPOINT_URL: (document.getElementById("s3-endpoint-url")?.value || "").trim(),
    };
    try {
        const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            setStatus('<span class="flash flash--success flash--inline">✓ Image storage saved. New uploads use this backend.</span>');
        } else {
            setStatus(`<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`);
        }
    } catch (e) {
        setStatus(`<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`);
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

async function saveCurrencySettings() {
    const status = document.getElementById("currency-settings-status");
    const body = {
        DEFAULT_CURRENCY: (document.getElementById("default-currency")?.value || "CAD").trim(),
        DEFAULT_CURRENCY_SYMBOL: (document.getElementById("default-currency-symbol")?.value || "$").trim(),
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
                '<span class="flash flash--success flash--inline">✓ Currency settings saved.</span>';
        } else {
            status.innerHTML =
                `<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`;
        }
    } catch (e) {
        status.innerHTML =
            `<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`;
    }
}

async function saveSmtpSettings() {
    const status = document.getElementById("smtp-settings-status");
    const smtpTls = document.getElementById("smtp-use-tls");
    const body = {
        SMTP_HOST: (document.getElementById("smtp-host")?.value || "").trim(),
        SMTP_PORT: parseInt(document.getElementById("smtp-port")?.value || "587", 10),
        SMTP_FROM: (document.getElementById("smtp-from")?.value || "").trim(),
        SMTP_USER: (document.getElementById("smtp-user")?.value || "").trim(),
        SMTP_PASSWORD: (document.getElementById("smtp-password")?.value || "").trim(),
        SMTP_USE_TLS: smtpTls ? smtpTls.checked : true,
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
                '<span class="flash flash--success flash--inline">✓ Outbound email (SMTP) settings saved.</span>';
        } else {
            status.innerHTML =
                `<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`;
        }
    } catch (e) {
        status.innerHTML =
            `<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`;
    }
}

async function saveMailSettings() {
    const status = document.getElementById("mail-settings-status");
    const setStatus = (html) => { if (status) status.innerHTML = html; };
    const body = {
        MAIL_BACKEND: (document.getElementById("mail-backend")?.value || "").trim(),
        MAIL_PROVIDER: (document.getElementById("mail-provider")?.value || "").trim(),
        MAIL_API_URL: (document.getElementById("mail-api-url")?.value || "").trim(),
        MAIL_API_KEY: (document.getElementById("mail-api-key")?.value || "").trim(),
        MAIL_FROM: (document.getElementById("mail-from")?.value || "").trim(),
    };
    try {
        const resp = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            setStatus('<span class="flash flash--success flash--inline">✓ Outbound email settings saved. Use “Send test email” to confirm delivery.</span>');
        } else {
            setStatus(`<span class="flash flash--error flash--inline">✗ Save failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`);
        }
    } catch (e) {
        setStatus(`<span class="flash flash--error flash--inline">✗ Save error: ${escapeHtml(e.message)}</span>`);
    }
}

async function loadMailUsage() {
    const box = document.getElementById("mail-usage");
    const status = document.getElementById("mail-usage-status");
    if (!box) return;
    if (status) status.innerHTML = "";
    try {
        const resp = await fetch("/api/settings/mail-usage");
        const data = await resp.json();
        if (!data.available) {
            box.innerHTML =
                `<p class="section-desc">Quota not available: ${escapeHtml(data.reason || "unknown reason")}.</p>`;
            return;
        }
        const u = data.usage;
        const row = (label, section) => {
            const used = section.used ?? 0;
            const limit = section.limit;
            if (limit === null || limit === undefined) {
                return `<div class="usage-row"><strong>${escapeHtml(label)}</strong>
                    <span>${used} used — no limit on this plan</span></div>`;
            }
            const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
            const cls = pct >= 80 ? "usage-bar--critical" : pct >= 50 ? "usage-bar--warn" : "";
            const resets = section.resets_at
                ? ` · resets ${new Date(section.resets_at).toLocaleString()}`
                : "";
            return `<div class="usage-row">
                <strong>${escapeHtml(label)}</strong>
                <span>${used} / ${limit} (${pct}%)${escapeHtml(resets)}</span>
                <div class="usage-bar"><div class="usage-bar-fill ${cls}" style="width:${pct}%"></div></div>
            </div>`;
        };
        const warn = u.critical
            ? '<div class="flash flash--error flash--inline">Quota is at 80% or more — new signups may fail soon.</div>'
            : "";
        box.innerHTML = warn
            + row("Last 24 hours", u.daily)
            + row("This month", u.monthly);
    } catch (e) {
        box.innerHTML = `<p class="flash flash--error">Could not load quota: ${escapeHtml(e.message)}</p>`;
    }
}

async function sendTestEmail() {
    const status = document.getElementById("smtp-test-status");
    const recipient = (document.getElementById("smtp-test-recipient")?.value || "").trim();
    const setStatus = (html) => { if (status) status.innerHTML = html; };
    if (!recipient || !recipient.includes("@")) {
        setStatus('<span class="flash flash--error flash--inline">✗ Enter a recipient address first.</span>');
        return;
    }
    setStatus('<span class="flash flash--inline">Sending…</span>');
    try {
        const resp = await fetch("/api/settings/test-email", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ to: recipient }),
        });
        const data = await resp.json();
        if (data.ok) {
            setStatus(`<span class="flash flash--success flash--inline">✓ ${escapeHtml(data.message)}</span>`);
        } else {
            setStatus(`<span class="flash flash--error flash--inline">✗ ${escapeHtml(data.error || ("HTTP " + resp.status))}</span>`);
        }
    } catch (e) {
        setStatus(`<span class="flash flash--error flash--inline">✗ ${escapeHtml(e.message)}</span>`);
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

function showUsersStatus(message, isError = false) {
    const el = document.getElementById("users-status");
    if (el) {
        el.innerHTML = `<span class="flash ${isError ? "flash--error" : "flash--success"} flash--inline">${message}</span>`;
    }
}

async function loadUsers() {
    const list = document.getElementById("users-list");
    if (!list) return;
    try {
        const resp = await fetch("/api/users");
        if (!resp.ok) {
            list.innerHTML = `<p class="flash flash--error">Could not load users (HTTP ${resp.status}). Admin access required.</p>`;
            return;
        }
        const data = await resp.json();
        const users = data.users || [];
        if (users.length === 0) {
            list.innerHTML = "<p>No users yet.</p>";
            return;
        }
        list.innerHTML = users.map(u => {
            // The local account is admin by definition, so it has no toggle.
            const isLocal = u.provider === "local";
            return `
            <div class="team-card">
                <div class="team-header">
                    <strong>${escapeHtml(u.name)}</strong>
                    <span class="team-role-badge team-role-badge--admin">${u.is_admin ? "admin" : escapeHtml(u.provider)}</span>
                </div>
                <div class="user-row">
                    <span class="member-email">${escapeHtml(u.email)}</span>
                    <span class="team-actions-right">
                        ${isLocal
                            ? '<span class="member-email">local admin — always admin</span>'
                            : `<button class="btn btn--sm ${u.is_admin ? "btn--danger" : "btn--primary"} user-admin-btn"
                                       data-user="${u.id}" data-name="${escapeAttr(u.name)}"
                                       data-next="${u.is_admin ? "0" : "1"}">
                                   ${u.is_admin ? "Revoke admin" : "Make admin"}
                               </button>`}
                    </span>
                </div>
            </div>`;
        }).join("");
        list.querySelectorAll(".user-admin-btn").forEach(btn => {
            btn.addEventListener("click", () =>
                setUserAdmin(btn.dataset.user, btn.dataset.next === "1", btn.dataset.name));
        });
    } catch (e) {
        list.innerHTML = `<p class="flash flash--error">Could not load users: ${escapeHtml(e.message)}</p>`;
    }
}

async function setUserAdmin(userId, makeAdmin, name) {
    try {
        const resp = await fetch(`/api/users/${userId}/admin`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_admin: makeAdmin }),
        });
        const data = await resp.json();
        if (data.ok) {
            showUsersStatus(`✓ ${escapeHtml(name)} is ${makeAdmin ? "now an admin" : "no longer an admin"}.`);
            loadUsers();
        } else {
            showUsersStatus(`✗ ${escapeHtml(data.error || "Could not update that user")}`, true);
        }
    } catch (e) {
        showUsersStatus(`✗ ${escapeHtml(e.message)}`, true);
    }
}

function showJoinRequestsStatus(message, isError = false) {
    const el = document.getElementById("join-requests-status");
    if (el) {
        el.innerHTML = message
            ? `<span class="flash ${isError ? "flash--error" : "flash--success"} flash--inline">${message}</span>`
            : "";
    }
}

async function loadJoinRequests() {
    const list = document.getElementById("join-requests-list");
    if (!list) return;
    try {
        const resp = await fetch("/api/join-requests");
        if (!resp.ok) {
            list.innerHTML = `<p class="flash flash--error">Could not load join requests (HTTP ${resp.status}).</p>`;
            return;
        }
        const data = await resp.json();
        const requests = data.requests || [];
        const pending = requests.filter(r => r.status === "pending");
        const badge = document.getElementById("join-requests-badge");
        if (badge) {
            badge.textContent = pending.length ? `${pending.length} pending` : "";
        }
        if (requests.length === 0) {
            list.innerHTML = "<p>No join requests yet.</p>";
            return;
        }
        list.innerHTML = requests.map(r => `
            <div class="team-card">
                <div class="team-header">
                    <strong>${escapeHtml(r.requester_name)}</strong>
                    <span class="team-role-badge team-role-badge--${escapeHtml(r.status)}">${escapeHtml(r.status)}</span>
                </div>
                <div class="member-email">${escapeHtml(r.requester_email)}</div>
                <p class="section-desc" style="margin: 6px 0;">${escapeHtml(r.description)}</p>
                ${r.status === "pending" ? `
                    <span class="team-actions-right">
                        <button class="btn btn--sm btn--primary join-approve" data-id="${r.id}"
                                data-name="${escapeAttr(r.requester_name)}">Approve</button>
                        <button class="btn btn--sm btn--danger join-deny" data-id="${r.id}"
                                data-name="${escapeAttr(r.requester_name)}">Decline</button>
                    </span>` : ""}
            </div>
        `).join("");
        list.querySelectorAll(".join-approve").forEach(btn => {
            btn.addEventListener("click", () => decideJoinRequest(btn.dataset.id, "approve", btn.dataset.name));
        });
        list.querySelectorAll(".join-deny").forEach(btn => {
            btn.addEventListener("click", () => decideJoinRequest(btn.dataset.id, "deny", btn.dataset.name));
        });
    } catch (e) {
        list.innerHTML = `<p class="flash flash--error">Could not load join requests: ${escapeHtml(e.message)}</p>`;
    }
}

async function decideJoinRequest(requestId, decision, name) {
    try {
        const resp = await fetch(`/api/join-requests/${requestId}/${decision}`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
            showJoinRequestsStatus(
                decision === "approve"
                    ? `✓ Approved — ${escapeHtml(name)} can now see that team's listings.`
                    : `✓ Declined ${escapeHtml(name)}'s request.`
            );
            loadJoinRequests();
            loadTeams();
        } else {
            showJoinRequestsStatus(`✗ ${escapeHtml(data.error || "Could not update that request")}`, true);
        }
    } catch (e) {
        showJoinRequestsStatus(`✗ ${escapeHtml(e.message)}`, true);
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
    wire("save-currency-settings", saveCurrencySettings);
    wire("save-smtp-settings", saveSmtpSettings);
    wire("save-mail-settings", saveMailSettings);
    wire("send-test-email", sendTestEmail);
    wire("refresh-mail-usage", loadMailUsage);
    wire("save-upload-settings", saveUploadSettings);
    wire("create-team", createTeam);
    loadAllSettings();
    loadTeams();
    loadUsers();
    loadJoinRequests();
    loadMailUsage();
});
