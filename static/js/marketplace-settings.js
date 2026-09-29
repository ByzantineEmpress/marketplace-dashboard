/**
 * Per-user marketplace accounts page.
 *
 * Loads the field description for each platform from the API rather than
 * hard-coding the forms here, so adding a marketplace is a server-side change.
 *
 * Two things this file is careful about:
 *
 *  1. Secret values are never written into the DOM. The API returns only
 *     `is_set` and a masked hint for secrets, and a secret input is rendered
 *     empty with the hint as its placeholder. There is nothing to leak and
 *     nothing for a password manager to read back.
 *
 *  2. A field left blank is not sent at all, so saving one platform cannot wipe
 *     another value the page never displayed.
 */
(function () {
    "use strict";

    const statusEl = document.getElementById("marketplace-status");
    // No getElementById on the dynamic parts: those nodes are created below, so
    // a static-id check would flag them as unwired.
    const root = document.getElementById("marketplace-platforms");
    if (!root) return;

    /** Human-readable platform names, falling back to capitalisation. */
    const ICONS = {
        ebay: "\u{1F6D2}",
        etsy: "\u{1F9F6}",
        poshmark: "\u{1F45C}",
        amazon: "\u{1F4E6}",
    };

    function setStatus(message, kind) {
        if (!statusEl) return;
        statusEl.textContent = message;
        statusEl.className = "settings-status" + (kind ? " settings-status--" + kind : "");
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    /** Per-platform panel builder, so the DOM is created not looked up. */
    function buildPanel(platform, info, accountsByPlatform) {
        const card = el("div", "team-card");
        card.dataset.platform = platform;
        card.style.marginTop = "14px";

        const header = el("div", "team-header");
        const title = el("strong", null, (ICONS[platform] ? ICONS[platform] + " " : "") + info.label);
        header.appendChild(title);

        const account = accountsByPlatform[platform];
        if (account && account.is_connected) {
            const badge = el("span", "card-status card-status--approved", "Connected");
            badge.style.marginLeft = "8px";
            header.appendChild(badge);
            if (account.shop_name) {
                header.appendChild(el("span", "section-desc", " \u2014 " + account.shop_name));
            }
        }
        card.appendChild(header);

        if (!info.is_configured) {
            card.appendChild(el(
                "p", "section-desc",
                "Add every field below to enable this marketplace for your account."
            ));
        }

        const inputs = {};
        info.fields.forEach(function (field) {
            const group = el("div", "form-group");
            const label = el("label", null, field.label);
            label.setAttribute("for", "cred-" + platform + "-" + field.key);
            group.appendChild(label);

            const input = document.createElement("input");
            input.type = field.is_secret ? "password" : "text";
            input.id = "cred-" + platform + "-" + field.key;
            input.autocomplete = "off";
            input.spellcheck = false;
            // Secrets are never filled in. The masked hint is the placeholder,
            // so the user can see something is stored without it being present.
            input.placeholder = field.is_set
                ? (field.is_secret ? field.hint + " (saved \u2014 leave blank to keep)" : "")
                : "";
            if (!field.is_secret && field.is_set) {
                input.placeholder = "(saved \u2014 leave blank to keep)";
            }
            group.appendChild(input);
            inputs[field.key] = input;
            card.appendChild(group);
        });

        const actions = el("div");
        actions.style.display = "flex";
        actions.style.gap = "8px";
        actions.style.marginTop = "6px";

        const saveBtn = el("button", "btn btn--primary", "Save");
        saveBtn.type = "button";

        const syncBtn = el("button", "btn btn--outline", "Sync now");
        syncBtn.type = "button";

        actions.appendChild(saveBtn);
        actions.appendChild(syncBtn);
        card.appendChild(actions);

        const note = el("div", "section-desc");
        note.style.marginTop = "6px";
        card.appendChild(note);

        saveBtn.addEventListener("click", function () {
            const values = {};
            Object.keys(inputs).forEach(function (key) {
                const raw = inputs[key].value;
                // Blank means "leave as-is": sending "" would clear it.
                if (raw === "") return;
                // A single space is the deliberate "clear this" signal.
                values[key] = raw === " " ? "" : raw;
            });
            if (Object.keys(values).length === 0) {
                note.textContent = "Nothing to save \u2014 all fields were left blank.";
                return;
            }
            saveBtn.disabled = true;
            note.textContent = "Saving\u2026";
            fetch("/api/accounts/credentials", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ platform: platform, values: values }),
            })
                .then(function (res) { return res.json().then(function (d) { return [res, d]; }); })
                .then(function (pair) {
                    const res = pair[0], data = pair[1];
                    if (!res.ok || !data.ok) {
                        note.textContent = "Could not save: " + (data.error || "HTTP " + res.status);
                        return;
                    }
                    const parts = [];
                    if (data.saved && data.saved.length) parts.push("saved " + data.saved.join(", "));
                    if (data.cleared && data.cleared.length) parts.push("cleared " + data.cleared.join(", "));
                    note.textContent = parts.length ? parts.join("; ") : "Nothing changed.";
                    setStatus("Credentials updated.", "ok");
                    load();
                })
                .catch(function (err) {
                    note.textContent = "Could not save: " + err.message;
                })
                .then(function () { saveBtn.disabled = false; });
        });

        syncBtn.addEventListener("click", function () {
            syncBtn.disabled = true;
            note.textContent = "Syncing\u2026";
            fetch("/api/accounts/sync", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ platform: platform }),
            })
                .then(function (res) { return res.json().then(function (d) { return [res, d]; }); })
                .then(function (pair) {
                    const res = pair[0], data = pair[1];
                    if (!res.ok || !data.ok) {
                        note.textContent = "Sync failed: " + (data.error || "HTTP " + res.status);
                        return;
                    }
                    const r = data.result || {};
                    if (r.errors && r.errors.length) {
                        note.textContent = "Sync reported: " + r.errors.join("; ");
                    } else {
                        note.textContent = "Synced " + (r.listings_fetched || 0) + " listing(s), "
                            + (r.listings_added || 0) + " new.";
                    }
                })
                .catch(function (err) {
                    note.textContent = "Sync failed: " + err.message;
                })
                .then(function () { syncBtn.disabled = false; });
        });

        return card;
    }

    function load() {
        Promise.all([
            fetch("/api/accounts/credentials").then(function (r) { return r.json(); }),
            fetch("/api/accounts").then(function (r) { return r.json(); }),
        ])
            .then(function (results) {
                const creds = results[0] || {};
                const accounts = Array.isArray(results[1]) ? results[1] : [];
                const accountsByPlatform = {};
                accounts.forEach(function (a) { accountsByPlatform[a.platform] = a; });

                root.textContent = "";
                const platforms = Object.keys(creds);
                if (platforms.length === 0) {
                    root.appendChild(el("p", "section-desc", "No marketplaces are available."));
                    return;
                }
                platforms.forEach(function (platform) {
                    root.appendChild(buildPanel(platform, creds[platform], accountsByPlatform));
                });
            })
            .catch(function (err) {
                setStatus("Could not load your accounts: " + err.message, "error");
            });
    }

    load();
})();
