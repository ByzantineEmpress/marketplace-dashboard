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
        actions.style.flexWrap = "wrap";
        actions.style.marginTop = "6px";

        const saveBtn = el("button", "btn btn--primary", "Save");
        saveBtn.type = "button";

        actions.appendChild(saveBtn);

        // Connect, Sync and Disconnect only mean something for a marketplace with
        // an OAuth flow. A plain API key (Google Books) has none: Connect would
        // invite a click that cannot work, Sync would have nothing to fetch, and
        // "Disconnect & delete data" would offer to delete listings it never had.
        if (info.connectable) {
            // Connect runs the marketplace's own OAuth flow. Necessary but not
            // sufficient to just save keys: the API credentials identify the
            // application, while an access token authorises reading THIS user's
            // shop. Without it a sync has nothing to authenticate with, which is
            // exactly the "no valid access token" message this button prevents.
            const connectBtn = el("button", "btn btn--success", "Connect");
            connectBtn.type = "button";
            if (!info.is_configured) {
                connectBtn.disabled = true;
                connectBtn.title = "Save your credentials first.";
            }

            const syncBtn = el("button", "btn btn--outline", "Sync now");
            syncBtn.type = "button";

            actions.appendChild(connectBtn);
            actions.appendChild(syncBtn);
        }

        // Disconnect & delete this marketplace's data. Shown when there is
        // anything to remove: either a live connection or saved credentials --
        // and only for a platform that has a connection to speak of.
        const hasAnything = info.connectable
            && ((account && account.is_connected) || info.is_configured);
        if (hasAnything) {
            const disconnectBtn = el("button", "btn btn--danger", "Disconnect & delete data");
            disconnectBtn.type = "button";
            disconnectBtn.style.marginLeft = "auto";
            disconnectBtn.addEventListener("click", function () {
                const ok = window.confirm(
                    "Disconnect " + info.label + " and delete its data?\n\n"
                    + "This removes the connection, your API keys for this platform, "
                    + "and the listings synced from it. It cannot be undone."
                );
                if (!ok) return;
                disconnectBtn.disabled = true;
                disconnectBtn.textContent = "Deleting\u2026";
                fetch("/api/accounts/" + encodeURIComponent(platform), { method: "DELETE" })
                    .then(function (res) { return res.json().then(function (d) { return [res, d]; }); })
                    .then(function (pair) {
                        const res = pair[0], data = pair[1];
                        if (!res.ok || !data.ok) {
                            note.textContent = "Could not disconnect: "
                                + (data.error || "HTTP " + res.status);
                            disconnectBtn.disabled = false;
                            disconnectBtn.textContent = "Disconnect & delete data";
                            return;
                        }
                        const d = data.deleted || {};
                        const bits = [];
                        if (d.marketplace_account) bits.push("connection");
                        if (d.credentials) bits.push("keys");
                        if (d.listings) bits.push(d.listings + " listing(s)");
                        setStatus("Disconnected " + info.label + (bits.length ? " (" + bits.join(", ") + ")." : "."), "ok");
                        load();
                    })
                    .catch(function (err) {
                        note.textContent = "Could not disconnect: " + err.message;
                        disconnectBtn.disabled = false;
                        disconnectBtn.textContent = "Disconnect & delete data";
                    });
            });
            actions.appendChild(disconnectBtn);
        }

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

        connectBtn.addEventListener("click", function () {
            connectBtn.disabled = true;
            note.textContent = "Starting authorisation\u2026";

            // Open the tab NOW, synchronously in the click handler. Browsers
            // only allow window.open during a user gesture, and the URL is not
            // known until the fetch below resolves. Opening a blank tab first
            // keeps the popup unblocked; its location is set once we have the URL.
            let authTab = null;
            try {
                authTab = window.open("", "_blank");
            } catch (e) {
                authTab = null;
            }

            function closeBlankTab() {
                if (authTab && !authTab.closed && authTab.location.href === "about:blank") {
                    try { authTab.close(); } catch (e) { /* ignore */ }
                }
            }

            fetch("/api/accounts/connect", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ platform: platform }),
            })
                .then(function (res) { return res.json().then(function (d) { return [res, d]; }); })
                .then(function (pair) {
                    const res = pair[0], data = pair[1];
                    if (!res.ok || !data.ok || !data.auth_url) {
                        note.textContent = "Could not start: "
                            + (data.error || "HTTP " + res.status);
                        connectBtn.disabled = false;
                        closeBlankTab();
                        return;
                    }
                    if (authTab) {
                        // Authorise in the new tab so this page keeps its place.
                        authTab.location.href = data.auth_url;
                        note.textContent = "Opened " + info.label
                            + " in a new tab \u2014 finish there, then press Sync here.";
                    } else {
                        // Popup blocked: fall back to navigating this tab so the
                        // user is never stuck.
                        note.textContent = "Redirecting to " + info.label + "\u2026";
                        window.location.assign(data.auth_url);
                        return;
                    }
                    // Re-enable: we are staying on this page, so the user must be
                    // able to retry. The state cookie is refreshed by each attempt.
                    connectBtn.disabled = false;
                })
                .catch(function (err) {
                    note.textContent = "Could not start: " + err.message;
                    connectBtn.disabled = false;
                    closeBlankTab();
                });
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
                        // The common case is "no token yet", so point at the
                        // button that fixes it rather than echoing jargon.
                        const needsConnect = r.errors.some(function (e) {
                            return /access token/i.test(e);
                        });
                        note.textContent = needsConnect
                            ? "Not authorised yet \u2014 press Connect to grant access to your shop."
                            : "Sync reported: " + r.errors.join("; ");
                    } else {
                        // Include the failure count. Reporting "0 new" while
                        // hundreds were rejected is exactly what hid a schema
                        // mismatch for several rounds.
                        let message = "Synced " + (r.listings_fetched || 0) + " listing(s), "
                            + (r.listings_added || 0) + " new";
                        if (r.listings_updated) {
                            message += ", " + r.listings_updated + " updated";
                        }
                        if (r.listings_failed) {
                            message += ", " + r.listings_failed + " FAILED";
                        }
                        note.textContent = message + ".";
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

    // -- Delete account & data (right to erasure) --
    const deleteBtn = document.getElementById("delete-account-btn");
    const deleteConfirm = document.getElementById("delete-account-confirm");
    const deleteInput = document.getElementById("delete-account-input");
    const deleteConfirmBtn = document.getElementById("delete-account-confirm-btn");
    const deleteStatus = document.getElementById("delete-account-status");

    if (deleteBtn && deleteConfirm && deleteInput && deleteConfirmBtn && deleteStatus) {
        deleteBtn.addEventListener("click", function () {
            deleteBtn.style.display = "none";
            deleteConfirm.style.display = "block";
            deleteInput.focus();
        });
        deleteInput.addEventListener("input", function () {
            deleteConfirmBtn.disabled = deleteInput.value.trim() !== "DELETE";
        });
        deleteConfirmBtn.addEventListener("click", async function () {
            if (deleteInput.value.trim() !== "DELETE") return;
            deleteConfirmBtn.disabled = true;
            deleteStatus.textContent = "Deleting\u2026";
            try {
                const res = await fetch("/api/account", { method: "DELETE" });
                const data = await res.json();
                if (!res.ok || !data.ok) {
                    throw new Error(data.error || "Deletion failed");
                }
                window.location.href = "/login?deleted=1";
            } catch (err) {
                deleteStatus.textContent = "Could not delete: " + err.message;
                deleteConfirmBtn.disabled = false;
            }
        });
    }

    load();
})();
