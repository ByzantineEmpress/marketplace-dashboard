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

    // Teams dropdown population
    async function loadTeams() {
        const sel = document.getElementById("filter-team");
        if (!sel) return;
        try {
            const res = await fetch("/api/teams");
            if (!res.ok) return;
            const teams = await res.json();
            if (Array.isArray(teams)) {
                loadedTeams = teams;
                if (teams.length >= 2) {
                    sel.innerHTML = '<option value="">All My Teams</option>';
                    teams.forEach(t => {
                        const opt = document.createElement("option");
                        opt.value = String(t.id);
                        opt.textContent = t.name;
                        sel.appendChild(opt);
                    });
                    sel.style.display = "";
                    sel.addEventListener("change", (e) => {
                        state.team = e.target.value;
                        state.page = 1;
                        loadListings();
                        loadStats();
                    });
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
        const actionWrap = document.getElementById("modal-action-wrap");

        if (teamSelect) {
            teamSelect.innerHTML = teamOptions;
        }
        if (statusSpan) {
            statusSpan.textContent = "";
        }
        if (actionWrap) {
            actionWrap.innerHTML = listing.original_url ? `
                <a href="${listing.original_url}" target="_blank" rel="noopener noreferrer" class="btn btn--sm btn--primary">
                    View on ${listing.platform ? listing.platform.toUpperCase() : 'Marketplace'} ↗
                </a>
            ` : '';
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
            });
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
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && modalBackdrop && modalBackdrop.classList.contains("is-open")) {
            closeModal();
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
    const icons = {
        ebay: '<span class="platform-icon" style="color:#e53238;font-weight:bold">ebay</span>',
        etsy: '<span class="platform-icon" style="color:#F56400;font-weight:bold">Etsy</span>',
    };
    return icons[platform] || `<span class="platform-icon">${escapeHtml(platform || '')}</span>`;
}
