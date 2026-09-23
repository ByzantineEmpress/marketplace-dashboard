/* ================================================================
   Dashboard page logic — Modernized
   Loads listings/stats, manages search/filters/pagination,
   and handles the interactive listing details modal.
   ================================================================ */

document.addEventListener("DOMContentLoaded", () => {
    const state = {
        page: 1,
        search: "",
        platform: "",
        status: "",
        team: "",
        sort: "created_at",
        order: "desc",
        total: 0,
        pageSize: 50,
    };

    let loadedListings = {};
    let loadedTeams = [];

    const grid = document.getElementById("listing-grid");
    const pageInfo = document.getElementById("page-info");
    const prevBtn = document.getElementById("prev-page");
    const nextBtn = document.getElementById("next-page");

    // Modal elements
    const modalBackdrop = document.getElementById("listing-modal-backdrop");
    const modalCloseBtn = document.getElementById("modal-close-btn");
    const modalPlatformIcon = document.getElementById("modal-platform-icon");
    const modalTitle = document.getElementById("modal-title");
    const modalBody = document.getElementById("modal-body");
    const modalFooter = document.getElementById("modal-footer");

    async function loadListings() {
        const params = new URLSearchParams({
            page: state.page,
            search: state.search,
            platform: state.platform,
            status: state.status,
            team: state.team,
            sort: state.sort,
            order: state.order,
        });

        try {
            grid.style.opacity = "0.6";
            const res = await fetch(`/api/listings?${params}`);
            if (res.status === 401) {
                window.location.href = "/login?error=session_expired";
                return;
            }
            const data = await res.json();
            grid.style.opacity = "1";

            state.total = data.total || 0;
            state.pageSize = data.page_size || 50;
            const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));

            // Cache for modal lookup
            loadedListings = {};
            (data.listings || []).forEach(item => {
                loadedListings[item.id] = item;
            });

            if (!data.listings || data.listings.length === 0) {
                renderEmptyState();
            } else {
                grid.innerHTML = data.listings.map(listing => renderCard(listing)).join("");
                // Wire click events on cards
                grid.querySelectorAll(".listing-card").forEach(card => {
                    card.addEventListener("click", () => {
                        const id = Number(card.dataset.id);
                        if (loadedListings[id]) {
                            openListingModal(loadedListings[id]);
                        }
                    });
                });
            }

            // Update pagination UI
            pageInfo.textContent = `Page ${state.page} of ${totalPages} (${state.total} listings)`;
            if (prevBtn) prevBtn.disabled = state.page <= 1;
            if (nextBtn) nextBtn.disabled = state.page >= totalPages;

        } catch (e) {
            grid.style.opacity = "1";
            grid.innerHTML = `<div class="empty-state"><p class="empty-state-text">Could not load listings: ${escapeHtml(e.message)}</p></div>`;
        }
    }

    function renderEmptyState() {
        const isFiltering = state.search || state.platform || state.status || state.team;
        if (isFiltering) {
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">
                        <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    </div>
                    <h3 class="empty-state-title">No matching listings</h3>
                    <p class="empty-state-text">No listings matched your active filters or search terms. Try clearing your filters.</p>
                    <button class="btn btn--sm clear-filters-btn">Clear All Filters</button>
                </div>
            `;
            const clearBtn = grid.querySelector(".clear-filters-btn");
            if (clearBtn) {
                clearBtn.addEventListener("click", () => {
                    state.search = "";
                    state.platform = "";
                    state.status = "";
                    state.team = "";
                    state.page = 1;
                    document.getElementById("search-input").value = "";
                    document.getElementById("filter-platform").value = "";
                    document.getElementById("filter-status").value = "";
                    const teamSel = document.getElementById("filter-team");
                    if (teamSel) teamSel.value = "";
                    loadListings();
                    loadStats();
                });
            }
        } else {
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">
                        <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>
                    </div>
                    <h3 class="empty-state-title">No listings synced yet</h3>
                    <p class="empty-state-text">Connect your eBay or Etsy seller account in Admin settings to automatically sync all your active listings.</p>
                    <a href="/admin" class="btn btn--primary">Go to Admin Settings</a>
                </div>
            `;
        }
    }

    async function loadStats() {
        try {
            const teamParam = state.team ? `?team=${state.team}` : "";
            const res = await fetch(`/api/stats${teamParam}`);
            if (!res.ok) return;
            const stats = await res.json();
            document.getElementById("stat-active").textContent = stats.active_listings || 0;
            document.getElementById("stat-sold").textContent = stats.sold_listings || 0;
            document.getElementById("stat-total").textContent = stats.total_listings || 0;
            if (stats.total_value_cents !== undefined) {
                const total = (stats.total_value_cents / 100).toLocaleString(undefined, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                });
                document.getElementById("stat-value").textContent = `$${total}`;
            }
        } catch (e) { /* ignore */ }
    }

    // Teams dropdown and modal population
    async function loadTeams() {
        const sel = document.getElementById("filter-team");
        const manualTeamSel = document.getElementById("manual-team");
        const teamsContainer = document.getElementById("dashboard-teams-container");
        const inviteUrlInput = document.getElementById("share-invite-url");
        try {
            const res = await fetch("/api/teams");
            if (!res.ok) return;
            const teams = await res.json();
            if (Array.isArray(teams)) {
                loadedTeams = teams;

                if (sel) {
                    if (teams.length >= 2) {
                        sel.innerHTML = '<option value="">All My Teams</option>';
                        teams.forEach(t => {
                            const opt = document.createElement("option");
                            opt.value = String(t.id);
                            opt.textContent = t.name;
                            if (state.team === String(t.id)) opt.selected = true;
                            sel.appendChild(opt);
                        });
                        sel.style.display = "";
                    } else {
                        sel.style.display = "none";
                    }
                }

                if (manualTeamSel) {
                    manualTeamSel.innerHTML = "";
                    teams.forEach(t => {
                        const opt = document.createElement("option");
                        opt.value = String(t.id);
                        opt.textContent = t.name;
                        manualTeamSel.appendChild(opt);
                    });
                }

                if (inviteUrlInput && teams.length > 0) {
                    const activeTeam = teams.find(t => String(t.id) === state.team) || teams[0];
                    inviteUrlInput.value = activeTeam.invite_url || "";
                }

                if (teamsContainer) {
                    if (teams.length === 0) {
                        teamsContainer.innerHTML = "<p style='color:var(--text-muted);'>No teams yet.</p>";
                    } else {
                        teamsContainer.innerHTML = teams.map(t => `
                            <div class="team-card" style="padding: 10px 12px; margin-bottom: 8px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg);">
                                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                                    <strong>${escapeHtml(t.name)}</strong>
                                    <span class="team-role-badge team-role-badge--${escapeHtml(t.role)}">${escapeHtml(t.role)}</span>
                                </div>
                                <div style="font-size: 12px; color: var(--text-muted);">
                                    Members: ${t.members.map(m => escapeHtml(m.name || m.email)).join(", ")}
                                </div>
                            </div>
                        `).join("");
                    }
                }
            }
        } catch (e) { /* single-team setups */ }
    }

    // --- Interactive Listing Detail Modal ---
    function openListingModal(listing) {
        if (!modalBackdrop) return;

        modalPlatformIcon.innerHTML = getPlatformIcon(listing.platform);
        modalTitle.textContent = listing.title || "Listing Details";

        const price = listing.price_raw || `$${((listing.price_cents || 0) / 100).toFixed(2)}`;
        const image = listing.image_url || "/static/img/placeholder.svg";

        let teamOptions = `<option value="">Shared (No team)</option>`;
        loadedTeams.forEach(t => {
            const selected = listing.team_id === t.id ? "selected" : "";
            teamOptions += `<option value="${t.id}" ${selected}>${escapeHtml(t.name)}</option>`;
        });

        modalBody.innerHTML = `
            <div class="modal-media">
                <img src="${image}" alt="${escapeHtml(listing.title || 'Listing')}" loading="lazy" onerror="this.src='/static/img/placeholder.svg'">
            </div>
            <div class="modal-price-row">
                <div class="modal-price">${price} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">${listing.currency || 'USD'}</span></div>
                <div>
                    <span class="card-status card-status--${listing.status || 'unknown'}">${listing.status || 'unknown'}</span>
                    ${listing.is_sold ? '<span class="card-status card-status--sold">Sold</span>' : ''}
                </div>
            </div>
            <div class="modal-details-grid">
                <div class="modal-detail-item">
                    <span class="modal-detail-label">Platform</span>
                    <span class="modal-detail-value">${listing.platform ? listing.platform.toUpperCase() : '—'}</span>
                </div>
                <div class="modal-detail-item">
                    <span class="modal-detail-label">Quantity</span>
                    <span class="modal-detail-value">${listing.available_quantity ?? '—'}</span>
                </div>
                <div class="modal-detail-item">
                    <span class="modal-detail-label">Views</span>
                    <span class="modal-detail-value">${listing.views_count ?? '0'}</span>
                </div>
                <div class="modal-detail-item">
                    <span class="modal-detail-label">SKU</span>
                    <span class="modal-detail-value">${escapeHtml(listing.sku || 'None')}</span>
                </div>
            </div>
            ${listing.description ? `
                <div>
                    <span class="modal-detail-label" style="display:block;margin-bottom:6px;">Description</span>
                    <div class="modal-description">${escapeHtml(listing.description)}</div>
                </div>
            ` : ''}
        `;

        const teamSelect = document.getElementById("modal-team-dropdown");
        const statusSpan = document.getElementById("modal-team-status");
        const markSoldBtn = document.getElementById("modal-mark-sold-btn");
        const deleteBtn = document.getElementById("modal-delete-btn");

        if (teamSelect) {
            teamSelect.innerHTML = teamOptions;
        }
        if (statusSpan) {
            statusSpan.textContent = "";
        }

        if (markSoldBtn) {
            markSoldBtn.style.display = listing.is_sold ? "none" : "";
            markSoldBtn.onclick = async () => {
                try {
                    const res = await fetch(`/api/listings/${listing.id}/mark-sold`, { method: "POST" });
                    const resData = await res.json();
                    if (resData.ok) {
                        listing.is_sold = true;
                        listing.status = "sold";
                        closeModal();
                        loadListings();
                        loadStats();
                    }
                } catch (err) {
                    alert("Could not mark listing as sold: " + err.message);
                }
            };
        }

        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete listing "${listing.title}"?`)) return;
                try {
                    const res = await fetch(`/api/listings/${listing.id}`, { method: "DELETE" });
                    const resData = await res.json();
                    if (resData.ok) {
                        closeModal();
                        loadListings();
                        loadStats();
                    }
                } catch (err) {
                    alert("Could not delete listing: " + err.message);
                }
            };
        }

        // Wire team change in modal
        if (teamSelect) {
            teamSelect.onchange = async (e) => {
                const newTeamId = e.target.value ? Number(e.target.value) : null;
                if (statusSpan) statusSpan.textContent = "Updating…";
                try {
                    const res = await fetch(`/api/listings/${listing.id}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ team_id: newTeamId }),
                    });
                    const resData = await res.json();
                    if (resData.ok) {
                        listing.team_id = newTeamId;
                        const teamObj = loadedTeams.find(t => t.id === newTeamId);
                        listing.team_name = teamObj ? teamObj.name : "Shared";
                        if (statusSpan) statusSpan.textContent = "✓ Saved";
                        loadListings();
                        loadStats();
                    } else {
                        if (statusSpan) statusSpan.textContent = "✗ Error";
                    }
                } catch (err) {
                    if (statusSpan) statusSpan.textContent = "✗ Network error";
                }
            };
        }

        modalBackdrop.classList.add("is-open");
        modalBackdrop.setAttribute("aria-hidden", "false");
    }

    function closeModal() {
        if (!modalBackdrop) return;
        modalBackdrop.classList.remove("is-open");
        modalBackdrop.setAttribute("aria-hidden", "true");
    }

    if (modalCloseBtn) modalCloseBtn.addEventListener("click", closeModal);
    if (modalBackdrop) {
        modalBackdrop.addEventListener("click", (e) => {
            if (e.target === modalBackdrop) closeModal();
        });
    }

    // --- Manual Listing Modal ---
    const manualModal = document.getElementById("manual-listing-modal");
    const openManualBtn = document.getElementById("add-manual-listing-btn");
    const closeManualBtn = document.getElementById("close-manual-modal-btn");
    const cancelManualBtn = document.getElementById("cancel-manual-modal-btn");
    const manualForm = document.getElementById("manual-listing-form");

    function openManualModal() {
        if (!manualModal) return;
        manualModal.style.display = "flex";
        manualModal.classList.add("is-open");
    }

    function closeManualModal() {
        if (!manualModal) return;
        manualModal.style.display = "none";
        manualModal.classList.remove("is-open");
    }

    if (openManualBtn) openManualBtn.addEventListener("click", openManualModal);
    if (closeManualBtn) closeManualBtn.addEventListener("click", closeManualModal);
    if (cancelManualBtn) cancelManualBtn.addEventListener("click", closeManualModal);

    if (manualForm) {
        manualForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const payload = {
                title: document.getElementById("manual-title").value.trim(),
                price: parseFloat(document.getElementById("manual-price").value || 0),
                quantity: parseInt(document.getElementById("manual-quantity").value || 1),
                platform: document.getElementById("manual-platform").value,
                status: document.getElementById("manual-status").value,
                sku: document.getElementById("manual-sku").value.trim(),
                team_id: document.getElementById("manual-team").value ? parseInt(document.getElementById("manual-team").value) : null,
                description: document.getElementById("manual-desc").value.trim(),
            };

            try {
                const res = await fetch("/api/listings/manual", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (data.ok) {
                    manualForm.reset();
                    closeManualModal();
                    loadListings();
                    loadStats();
                } else {
                    alert("Could not save listing: " + (data.error || "Unknown error"));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            }
        });
    }

    // --- Teams & Invites Modal ---
    const teamsModal = document.getElementById("teams-modal");
    const openTeamsBtn = document.getElementById("open-teams-modal-btn");
    const closeTeamsBtn = document.getElementById("close-teams-modal-btn");
    const closeTeamsFooterBtn = document.getElementById("close-teams-modal-footer-btn");
    const copyInviteBtn = document.getElementById("copy-invite-link-btn");
    const copyFeedback = document.getElementById("copy-feedback");
    const sendInviteBtn = document.getElementById("send-email-invite-btn");
    const inviteEmailInput = document.getElementById("invite-email-input");
    const inviteEmailStatus = document.getElementById("invite-email-status");
    const createTeamBtn = document.getElementById("dashboard-create-team-btn");
    const newTeamNameInput = document.getElementById("dashboard-new-team-name");
    const createTeamStatus = document.getElementById("dashboard-create-team-status");

    function openTeamsModal() {
        if (!teamsModal) return;
        teamsModal.style.display = "flex";
        teamsModal.classList.add("is-open");
        loadTeams();
    }

    function closeTeamsModal() {
        if (!teamsModal) return;
        teamsModal.style.display = "none";
        teamsModal.classList.remove("is-open");
    }

    if (openTeamsBtn) openTeamsBtn.addEventListener("click", openTeamsModal);
    if (closeTeamsBtn) closeTeamsBtn.addEventListener("click", closeTeamsModal);
    if (closeTeamsFooterBtn) closeTeamsFooterBtn.addEventListener("click", closeTeamsModal);

    if (copyInviteBtn) {
        copyInviteBtn.addEventListener("click", async () => {
            const urlInput = document.getElementById("share-invite-url");
            if (urlInput && urlInput.value) {
                try {
                    await navigator.clipboard.writeText(urlInput.value);
                    if (copyFeedback) {
                        copyFeedback.style.display = "inline-block";
                        setTimeout(() => { copyFeedback.style.display = "none"; }, 2500);
                    }
                } catch (e) {
                    urlInput.select();
                    document.execCommand("copy");
                }
            }
        });
    }

    if (sendInviteBtn) {
        sendInviteBtn.addEventListener("click", async () => {
            const email = (inviteEmailInput?.value || "").trim();
            if (!email) return;
            const teamId = state.team || (loadedTeams[0] ? loadedTeams[0].id : null);
            if (!teamId) {
                if (inviteEmailStatus) inviteEmailStatus.innerHTML = "<span class='flash flash--error flash--inline'>Create or select a team first.</span>";
                return;
            }
            try {
                const res = await fetch(`/api/teams/${teamId}/members`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ email }),
                });
                const data = await res.json();
                if (data.ok) {
                    if (inviteEmailInput) inviteEmailInput.value = "";
                    if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--success flash--inline'>✓ Added ${escapeHtml(email)}!</span>`;
                    loadTeams();
                } else {
                    if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(data.error || "Failed to add")}</span>`;
                }
            } catch (err) {
                if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(err.message)}</span>`;
            }
        });
    }

    if (createTeamBtn) {
        createTeamBtn.addEventListener("click", async () => {
            const name = (newTeamNameInput?.value || "").trim();
            if (!name) return;
            try {
                const res = await fetch("/api/teams", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name }),
                });
                const data = await res.json();
                if (data.ok) {
                    if (newTeamNameInput) newTeamNameInput.value = "";
                    if (createTeamStatus) createTeamStatus.innerHTML = `<span class='flash flash--success flash--inline'>✓ Team "${escapeHtml(name)}" created!</span>`;
                    await loadTeams();
                    loadListings();
                } else {
                    if (createTeamStatus) createTeamStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(data.error || "Failed")}</span>`;
                }
            } catch (err) {
                if (createTeamStatus) createTeamStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(err.message)}</span>`;
            }
        });
    }

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (modalBackdrop && modalBackdrop.classList.contains("is-open")) closeModal();
            if (manualModal && manualModal.classList.contains("is-open")) closeManualModal();
            if (teamsModal && teamsModal.classList.contains("is-open")) closeTeamsModal();
        }
    });

    // Event: search input (debounced)
    let searchTimeout;
    document.getElementById("search-input").addEventListener("input", (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            state.search = e.target.value;
            state.page = 1;
            loadListings();
        }, 300);
    });

    // Event: platform filter
    document.getElementById("filter-platform").addEventListener("change", (e) => {
        state.platform = e.target.value;
        state.page = 1;
        loadListings();
    });

    // Event: team filter
    const teamFilterEl = document.getElementById("filter-team");
    if (teamFilterEl) {
        teamFilterEl.addEventListener("change", (e) => {
            state.team = e.target.value;
            state.page = 1;
            loadListings();
            loadStats();
        });
    }

    // Event: status filter
    document.getElementById("filter-status").addEventListener("change", (e) => {
        state.status = e.target.value;
        state.page = 1;
        loadListings();
    });

    // Event: refresh button
    document.getElementById("refresh-btn").addEventListener("click", () => {
        loadListings();
        loadStats();
    });

    // Event: pagination
    if (prevBtn) {
        prevBtn.addEventListener("click", () => {
            if (state.page > 1) {
                state.page--;
                loadListings();
            }
        });
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", () => {
            const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
            if (state.page < totalPages) {
                state.page++;
                loadListings();
            }
        });
    }

    // Initial load
    loadStats();
    loadTeams();
    loadListings();
});

// Render a single listing card
function renderCard(listing) {
    const platformIcon = getPlatformIcon(listing.platform);
    const price = listing.price_raw || `$${((listing.price_cents || 0) / 100).toFixed(2)}`;
    const image = listing.image_url || "/static/img/placeholder.svg";

    return `
        <div class="listing-card" data-id="${listing.id}" data-platform="${listing.platform}">
            <div class="card-platform-badge">${platformIcon}</div>
            <div class="card-image">
                <img src="${image}" alt="${escapeHtml(listing.title || 'Listing')}" loading="lazy" onerror="this.src='/static/img/placeholder.svg'">
            </div>
            <div class="card-body">
                <h3 class="card-title">${escapeHtml(listing.title || "Untitled")}</h3>
                <p class="card-price">${price}</p>
                <div class="card-meta">
                    <span class="card-status card-status--${listing.status || 'unknown'}">${listing.status || "unknown"}</span>
                    ${listing.team_name ? `<span class="card-team" title="Team">${escapeHtml(listing.team_name)}</span>` : ""}
                </div>
            </div>
        </div>
    `;
}

function getPlatformIcon(platform) {
    const p = (platform || '').toLowerCase();
    const icons = {
        ebay: '<span class="platform-icon" style="color:#e53238;font-weight:bold">eBay</span>',
        etsy: '<span class="platform-icon" style="color:#F56400;font-weight:bold">Etsy</span>',
        poshmark: '<span class="platform-icon" style="color:#8E1A34;font-weight:bold">Poshmark</span>',
        amazon: '<span class="platform-icon" style="color:#FF9900;font-weight:bold">Amazon</span>',
        local: '<span class="platform-icon" style="color:#10b981;font-weight:bold">Local</span>',
        facebook: '<span class="platform-icon" style="color:#1877F2;font-weight:bold">FB Market</span>',
        craigslist: '<span class="platform-icon" style="color:#795548;font-weight:bold">Craigslist</span>',
    };
    return icons[p] || `<span class="platform-icon">${escapeHtml(platform || '')}</span>`;
}
