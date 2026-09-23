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
        metricsDays: "all",
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

    // Metrics time-range filter
    const metricsPeriodBadge = document.getElementById("metrics-period-badge");
    const metricsFilterGroup = document.getElementById("metrics-filter-group");
    const metricsBtn7d = document.getElementById("metrics-btn-7d");
    const metricsBtn30d = document.getElementById("metrics-btn-30d");
    const metricsBtnAll = document.getElementById("metrics-btn-all");

    function setMetricsPeriod(days) {
        state.metricsDays = days;
        const btns = [
            { el: metricsBtn7d, val: "7", label: "Last 7 Days" },
            { el: metricsBtn30d, val: "30", label: "Last 30 Days" },
            { el: metricsBtnAll, val: "all", label: "All Time" },
        ];
        btns.forEach(b => {
            if (b.el) {
                if (b.val === days) {
                    b.el.classList.add("is-active");
                    if (metricsPeriodBadge) metricsPeriodBadge.textContent = b.label;
                } else {
                    b.el.classList.remove("is-active");
                }
            }
        });
        loadStats();
    }

    if (metricsBtn7d) metricsBtn7d.addEventListener("click", () => setMetricsPeriod("7"));
    if (metricsBtn30d) metricsBtn30d.addEventListener("click", () => setMetricsPeriod("30"));
    if (metricsBtnAll) metricsBtnAll.addEventListener("click", () => setMetricsPeriod("all"));

    async function loadStats() {
        try {
            const params = new URLSearchParams();
            if (state.team) params.set("team", state.team);
            if (state.metricsDays && state.metricsDays !== "all") {
                params.set("days", state.metricsDays);
            }
            const query = params.toString() ? `?${params.toString()}` : "";
            const res = await fetch(`/api/stats${query}`);
            if (!res.ok) return;
            const stats = await res.json();
            const cur = stats.currency || "CAD";
            const sym = stats.currency_symbol || "$";

            const statActive = document.getElementById("stat-active");
            if (statActive) statActive.textContent = stats.active_listings || 0;

            const statSold = document.getElementById("stat-sold");
            if (statSold) statSold.textContent = stats.sold_listings || 0;

            const statTotal = document.getElementById("stat-total");
            if (statTotal) statTotal.textContent = stats.total_listings || 0;

            if (stats.total_value_cents !== undefined) {
                const total = (stats.total_value_cents / 100).toLocaleString(undefined, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                });
                const statVal = document.getElementById("stat-value");
                if (statVal) statVal.textContent = `${sym}${total} ${cur}`;
            }

            if (stats.total_cost_cents !== undefined) {
                const cost = (stats.total_cost_cents / 100).toLocaleString(undefined, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                });
                const statCost = document.getElementById("stat-cost");
                if (statCost) statCost.textContent = `${sym}${cost} ${cur}`;
            }

            if (stats.sold_profit_cents !== undefined) {
                const profitVal = (stats.sold_profit_cents || 0) / 100;
                const profitStr = Math.abs(profitVal).toLocaleString(undefined, {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                });
                const prefix = profitVal < 0 ? `-${sym}` : (profitVal > 0 ? `+${sym}` : `${sym}`);
                const statProfit = document.getElementById("stat-profit");
                if (statProfit) {
                    statProfit.textContent = `${prefix}${profitStr} ${cur}`;
                    statProfit.style.color = profitVal > 0 ? "var(--success)" : (profitVal < 0 ? "var(--danger)" : "var(--text)");
                }
            }
        } catch (e) { /* ignore */ }
    }

    // Teams dropdown and modal population
    async function loadTeams() {
        const sel = document.getElementById("filter-team");
        const manualTeamSel = document.getElementById("manual-team");
        const inviteTeamSel = document.getElementById("invite-team-select");
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

                if (inviteTeamSel) {
                    inviteTeamSel.innerHTML = "";
                    teams.forEach(t => {
                        const opt = document.createElement("option");
                        opt.value = String(t.id);
                        opt.textContent = t.name;
                        if (state.team === String(t.id)) opt.selected = true;
                        inviteTeamSel.appendChild(opt);
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

        modalPlatformIcon.innerHTML = renderPlatformBadges(listing.platforms, listing.platform);
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
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px; margin-bottom: 12px; gap: 8px; flex-wrap: wrap;">
                <span style="font-size: 12px; color: var(--text-muted);">Photo:</span>
                <div style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap;">
                    <input type="file" class="detail-camera-file" accept="image/*" capture="environment" style="display: none;">
                    <button type="button" class="btn btn--sm btn--outline detail-camera-btn" style="font-size: 11.5px; padding: 4px 8px;">📷 Take Photo</button>
                    <input type="file" class="detail-image-file" accept="image/*" style="display: none;">
                    <button type="button" class="btn btn--sm btn--outline detail-upload-img-btn" style="font-size: 11.5px; padding: 4px 8px;">🖼️ Gallery</button>
                    <input type="text" class="detail-image-url-input" placeholder="Or paste image URL" value="${escapeHtml(listing.image_url || '')}" style="font-size: 12px; padding: 4px 8px; width: 150px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);">
                </div>
            </div>
            <div class="modal-price-row">
                <div class="modal-price">${price} <span style="font-size:14px;font-weight:normal;color:var(--text-muted);">${listing.currency || 'CAD'}</span></div>
                <div>
                    <span class="card-status card-status--${listing.status || 'unknown'}">${listing.status || 'unknown'}</span>
                    ${listing.is_sold ? '<span class="card-status card-status--sold">Sold</span>' : ''}
                </div>
            </div>

            <!-- Multi-Channel Cross-Listing Section -->
            <div class="modal-platforms-section" style="margin-bottom: 14px; padding: 12px 14px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <h4 style="margin: 0; font-size: 13.5px; font-weight: 600;">🏷️ Listed On (Channels &amp; Marketplaces)</h4>
                    <span style="font-size: 11.5px; color: var(--text-muted);">Mark all places where this is active</span>
                </div>
                <div class="detail-platforms-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 6px;">
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="ebay" ${hasPlatform(listing, 'ebay') ? 'checked' : ''}>
                        <span style="color:#e53238; font-weight:bold;">eBay</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="etsy" ${hasPlatform(listing, 'etsy') ? 'checked' : ''}>
                        <span style="color:#F56400; font-weight:bold;">Etsy</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="facebook" ${hasPlatform(listing, 'facebook') ? 'checked' : ''}>
                        <span style="color:#1877F2; font-weight:bold;">FB Marketplace</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="local" ${hasPlatform(listing, 'local') ? 'checked' : ''}>
                        <span style="color:#10b981; font-weight:bold;">Local / In-Person</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="poshmark" ${hasPlatform(listing, 'poshmark') ? 'checked' : ''}>
                        <span style="color:#8E1A34; font-weight:bold;">Poshmark</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="amazon" ${hasPlatform(listing, 'amazon') ? 'checked' : ''}>
                        <span style="color:#FF9900; font-weight:bold;">Amazon</span>
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer;">
                        <input type="checkbox" class="detail-platform-cb" value="craigslist" ${hasPlatform(listing, 'craigslist') ? 'checked' : ''}>
                        <span style="color:#795548; font-weight:bold;">Craigslist / Kijiji</span>
                    </label>
                </div>
            </div>

            <div class="modal-details-grid">
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
                <div class="modal-detail-item">
                    <span class="modal-detail-label">Platform</span>
                    <span class="modal-detail-value">${listing.platform ? listing.platform.toUpperCase() : '—'}</span>
                </div>
            </div>
            ${listing.description ? `
                <div style="margin-bottom: 14px;">
                    <span class="modal-detail-label" style="display:block;margin-bottom:6px;">Description</span>
                    <div class="modal-description">${escapeHtml(listing.description)}</div>
                </div>
            ` : ''}

            <!-- Costs, Parts & Profit Breakdown ($ CAD) -->
            <div class="modal-costs-section" style="margin-top: 14px; padding: 14px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <h4 style="margin: 0; font-size: 13.5px; font-weight: 600;">💰 Bought For &amp; Parts Put Into It ($ CAD)</h4>
                    <span style="font-size: 11.5px; color: var(--text-muted);">For all listings &amp; platforms</span>
                </div>
                <div style="display: flex; gap: 10px; margin-bottom: 12px;">
                    <div class="form-group" style="flex: 1; margin-bottom: 0;">
                        <label style="font-size: 12px; font-weight: 600; display: block; margin-bottom: 4px;">Bought For / Cost ($ CAD)</label>
                        <input type="number" step="0.01" min="0" class="detail-purchase-price-input" value="${(listing.purchase_price !== undefined ? listing.purchase_price : 0).toFixed(2)}" style="width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);">
                    </div>
                    <div class="form-group" style="flex: 1; margin-bottom: 0;">
                        <label style="font-size: 12px; font-weight: 600; display: block; margin-bottom: 4px;">Selling / Listed Price ($ CAD)</label>
                        <input type="number" step="0.01" min="0" class="detail-selling-price-input" value="${((listing.price_cents || 0) / 100).toFixed(2)}" style="width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);">
                    </div>
                </div>
                <div style="margin-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <label style="font-size: 12px; font-weight: 600; margin: 0;">Parts &amp; Repairs Added</label>
                        <button type="button" class="btn btn--sm btn--outline detail-add-part-btn" style="font-size: 11px; padding: 2px 7px;">+ Add Part</button>
                    </div>
                    <p style="font-size: 11.5px; color: var(--text-muted); margin-bottom: 6px;">
                        Log parts installed (e.g. "New power supply" - $25.00 CAD).
                    </p>
                    <div class="detail-parts-list" style="display: flex; flex-direction: column; gap: 6px;">
                        <!-- Dynamic parts injected here -->
                    </div>
                </div>
                <div class="detail-financial-summary" style="padding: 8px 10px; background: var(--bg-elevated); border: 1px dashed var(--border); border-radius: 6px; font-size: 12px; display: flex; justify-content: space-between; margin-bottom: 12px;">
                    <span>Total Investment: <strong class="detail-total-cost-val">$0.00 CAD</strong></span>
                    <span>Est. Net Profit: <strong class="detail-net-profit-val" style="color: var(--success);">+$0.00 CAD</strong> <span class="detail-margin-val" style="color: var(--text-muted);">(0%)</span></span>
                </div>
                <div style="display: flex; justify-content: flex-end; align-items: center; gap: 10px;">
                    <span class="detail-save-cost-status" style="font-size: 12px;"></span>
                    <button type="button" class="btn btn--sm btn--primary detail-save-cost-btn">💾 Save Changes (Costs, Channels &amp; Photo)</button>
                </div>
            </div>
        `;

        // Wire Cost & Parts Breakdown Editor
        const partsContainer = modalBody.querySelector(".detail-parts-list");
        const addPartBtn = modalBody.querySelector(".detail-add-part-btn");
        const purchaseInput = modalBody.querySelector(".detail-purchase-price-input");
        const sellingInput = modalBody.querySelector(".detail-selling-price-input");
        const totalCostVal = modalBody.querySelector(".detail-total-cost-val");
        const netProfitVal = modalBody.querySelector(".detail-net-profit-val");
        const marginVal = modalBody.querySelector(".detail-margin-val");
        const saveCostBtn = modalBody.querySelector(".detail-save-cost-btn");
        const saveCostStatus = modalBody.querySelector(".detail-save-cost-status");

        // Wire Photo Upload & Camera in detail modal
        const detailCameraFileInput = modalBody.querySelector(".detail-camera-file");
        const detailCameraBtn = modalBody.querySelector(".detail-camera-btn");
        const detailImgFileInput = modalBody.querySelector(".detail-image-file");
        const detailUploadImgBtn = modalBody.querySelector(".detail-upload-img-btn");
        const detailImgUrlInput = modalBody.querySelector(".detail-image-url-input");
        const modalImg = modalBody.querySelector(".modal-media img");

        async function uploadDetailImage(file, triggerBtn) {
            if (!file) return;
            const origText = triggerBtn ? triggerBtn.textContent : "";
            if (triggerBtn) {
                triggerBtn.textContent = "Uploading…";
                triggerBtn.disabled = true;
            }
            try {
                const formData = new FormData();
                formData.append("file", file);
                const res = await fetch("/api/upload", { method: "POST", body: formData });
                const data = await res.json();
                if (data.ok) {
                    if (detailImgUrlInput) detailImgUrlInput.value = data.url;
                    if (modalImg) modalImg.src = data.url;
                } else {
                    alert("Upload failed: " + (data.error || "Unknown error"));
                }
            } catch (err) {
                alert("Upload error: " + err.message);
            } finally {
                if (triggerBtn) {
                    triggerBtn.textContent = origText;
                    triggerBtn.disabled = false;
                }
            }
        }

        if (detailCameraBtn && detailCameraFileInput) {
            detailCameraBtn.onclick = () => detailCameraFileInput.click();
            detailCameraFileInput.onchange = (e) => {
                const file = e.target.files && e.target.files[0];
                uploadDetailImage(file, detailCameraBtn);
            };
        }

        if (detailUploadImgBtn && detailImgFileInput) {
            detailUploadImgBtn.onclick = () => detailImgFileInput.click();
            detailImgFileInput.onchange = (e) => {
                const file = e.target.files && e.target.files[0];
                uploadDetailImage(file, detailUploadImgBtn);
            };
        }

        if (detailImgUrlInput) {
            detailImgUrlInput.oninput = (e) => {
                if (modalImg && e.target.value.trim()) {
                    modalImg.src = e.target.value.trim();
                }
            };
        }

        function renderDetailPartRow(desc = "", cost = "") {
            if (!partsContainer) return;
            const row = document.createElement("div");
            row.className = "detail-part-row";
            row.style.cssText = "display: flex; gap: 6px; align-items: center;";
            row.innerHTML = `
                <input type="text" class="detail-part-desc" placeholder="Part description (e.g. New power supply)" value="${escapeHtml(desc)}" style="flex: 2; padding: 5px 8px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);" required>
                <input type="number" step="0.01" min="0" class="detail-part-cost" placeholder="0.00" value="${cost !== '' ? cost : ''}" style="flex: 1; padding: 5px 8px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);" required>
                <button type="button" class="btn btn--sm btn--danger detail-remove-part-btn" style="padding: 3px 7px; font-size: 12px;">&times;</button>
            `;
            partsContainer.appendChild(row);
        }

        const existingParts = Array.isArray(listing.parts) ? listing.parts : [];
        existingParts.forEach(p => {
            const c = p.cost !== undefined ? p.cost : ((p.cost_cents || 0) / 100);
            renderDetailPartRow(p.description || p.name || "", c ? c.toFixed(2) : "");
        });

        function updateDetailCalculations() {
            const purchasePrice = parseFloat(purchaseInput?.value || 0);
            let partsTotal = 0;
            if (partsContainer) {
                partsContainer.querySelectorAll(".detail-part-cost").forEach(input => {
                    partsTotal += parseFloat(input.value || 0);
                });
            }
            const totalCost = purchasePrice + partsTotal;
            const sellingPrice = parseFloat(sellingInput?.value || 0);
            const netProfit = sellingPrice - totalCost;
            const margin = sellingPrice > 0 ? ((netProfit / sellingPrice) * 100).toFixed(1) : "0.0";

            if (totalCostVal) totalCostVal.textContent = `$${totalCost.toFixed(2)} CAD`;
            if (netProfitVal) {
                netProfitVal.textContent = `${netProfit > 0 ? "+" : ""}$${netProfit.toFixed(2)} CAD`;
                netProfitVal.style.color = netProfit >= 0 ? "var(--success)" : "var(--danger)";
            }
            if (marginVal) {
                marginVal.textContent = `(${margin}%)`;
            }
        }

        updateDetailCalculations();

        if (addPartBtn) {
            addPartBtn.onclick = () => {
                renderDetailPartRow("", "");
                updateDetailCalculations();
            };
        }

        if (partsContainer) {
            partsContainer.oninput = () => updateDetailCalculations();
            partsContainer.onclick = (e) => {
                if (e.target.closest(".detail-remove-part-btn")) {
                    e.target.closest(".detail-part-row").remove();
                    updateDetailCalculations();
                }
            };
        }

        if (purchaseInput) purchaseInput.oninput = () => updateDetailCalculations();
        if (sellingInput) sellingInput.oninput = () => updateDetailCalculations();

        if (saveCostBtn) {
            saveCostBtn.onclick = async () => {
                saveCostBtn.disabled = true;
                if (saveCostStatus) saveCostStatus.innerHTML = "<span style='color:var(--text-muted);'>Saving…</span>";

                const parts = [];
                if (partsContainer) {
                    partsContainer.querySelectorAll(".detail-part-row").forEach(row => {
                        const desc = (row.querySelector(".detail-part-desc")?.value || "").trim();
                        const cost = parseFloat(row.querySelector(".detail-part-cost")?.value || 0);
                        if (desc) {
                            parts.push({ description: desc, cost });
                        }
                    });
                }

                const purchasePrice = parseFloat(purchaseInput?.value || 0);
                const sellingPrice = parseFloat(sellingInput?.value || 0);

                const checkedPlatforms = [];
                modalBody.querySelectorAll(".detail-platform-cb:checked").forEach(cb => {
                    checkedPlatforms.push(cb.value);
                });
                const imgUrl = (detailImgUrlInput?.value || "").trim();

                try {
                    const res = await fetch(`/api/listings/${listing.id}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            purchase_price: purchasePrice,
                            price: sellingPrice,
                            parts: parts,
                            platforms: checkedPlatforms,
                            image_url: imgUrl,
                        }),
                    });
                    const resData = await res.json();
                    if (resData.ok && resData.listing) {
                        loadedListings[listing.id] = { ...listing, ...resData.listing };
                        Object.assign(listing, resData.listing);

                        modalPlatformIcon.innerHTML = renderPlatformBadges(listing.platforms, listing.platform);

                        if (saveCostStatus) saveCostStatus.innerHTML = "<span class='flash flash--success flash--inline'>✓ Saved!</span>";
                        setTimeout(() => { if (saveCostStatus) saveCostStatus.innerHTML = ""; }, 2500);

                        // Update card in grid if visible
                        const card = document.querySelector(`.listing-card[data-id="${listing.id}"]`);
                        if (card) {
                            const newCardHtml = renderCard(listing);
                            const tmp = document.createElement("div");
                            tmp.innerHTML = newCardHtml;
                            const newCardEl = tmp.firstElementChild;
                            card.replaceWith(newCardEl);
                            newCardEl.addEventListener("click", () => openListingModal(listing));
                        }

                        loadStats();
                    } else {
                        if (saveCostStatus) saveCostStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(resData.error || "Save failed")}</span>`;
                    }
                } catch (err) {
                    if (saveCostStatus) saveCostStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(err.message)}</span>`;
                } finally {
                    saveCostBtn.disabled = false;
                }
            };
        }

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
    const manualAddPartBtn = document.getElementById("manual-add-part-btn");
    const manualPartsContainer = document.getElementById("manual-parts-container");
    const manualTotalCostPreview = document.getElementById("manual-total-cost-preview");
    const manualProfitPreview = document.getElementById("manual-profit-preview");
    const manualPriceInput = document.getElementById("manual-price");
    const manualPurchasePriceInput = document.getElementById("manual-purchase-price");

    function updateManualCalculations() {
        const price = parseFloat(manualPriceInput?.value || 0);
        const purchasePrice = parseFloat(manualPurchasePriceInput?.value || 0);
        let partsTotal = 0;
        if (manualPartsContainer) {
            manualPartsContainer.querySelectorAll(".manual-part-cost").forEach(input => {
                partsTotal += parseFloat(input.value || 0);
            });
        }
        const totalCost = purchasePrice + partsTotal;
        const profit = price - totalCost;

        if (manualTotalCostPreview) manualTotalCostPreview.textContent = `$${totalCost.toFixed(2)} CAD`;
        if (manualProfitPreview) {
            manualProfitPreview.textContent = `${profit > 0 ? "+" : ""}$${profit.toFixed(2)} CAD`;
            manualProfitPreview.style.color = profit >= 0 ? "var(--success)" : "var(--danger)";
        }
    }

    if (manualAddPartBtn && manualPartsContainer) {
        manualAddPartBtn.addEventListener("click", () => {
            const row = document.createElement("div");
            row.className = "manual-part-row";
            row.style.cssText = "display: flex; gap: 8px; align-items: center;";
            row.innerHTML = `
                <input type="text" class="manual-part-desc" placeholder="Part description (e.g. New power supply)" style="flex: 2; padding: 6px 8px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px;" required>
                <input type="number" step="0.01" min="0" class="manual-part-cost" placeholder="Cost ($ CAD)" style="flex: 1; padding: 6px 8px; font-size: 12px; border: 1px solid var(--border); border-radius: 4px;" required>
                <button type="button" class="btn btn--sm btn--danger manual-remove-part-btn" style="padding: 4px 8px; font-size: 13px;">&times;</button>
            `;
            manualPartsContainer.appendChild(row);
            updateManualCalculations();
        });

        manualPartsContainer.addEventListener("input", updateManualCalculations);
        manualPartsContainer.addEventListener("click", (e) => {
            if (e.target.closest(".manual-remove-part-btn")) {
                e.target.closest(".manual-part-row").remove();
                updateManualCalculations();
            }
        });
    }

    if (manualPriceInput) manualPriceInput.addEventListener("input", updateManualCalculations);
    if (manualPurchasePriceInput) manualPurchasePriceInput.addEventListener("input", updateManualCalculations);

    const manualCameraFileInput = document.getElementById("manual-camera-file");
    const manualTakePhotoBtn = document.getElementById("manual-take-photo-btn");
    const manualImageFileInput = document.getElementById("manual-image-file");
    const manualChooseFileBtn = document.getElementById("manual-choose-file-btn");
    const manualImageUrlInput = document.getElementById("manual-image-url");
    const manualImagePreviewWrap = document.getElementById("manual-image-preview-wrap");
    const manualImagePreview = document.getElementById("manual-image-preview");
    const manualImageRemoveBtn = document.getElementById("manual-image-remove-btn");
    const manualImageUploadStatus = document.getElementById("manual-image-upload-status");

    async function handleManualFileUpload(file) {
        if (!file) return;
        if (manualImageUploadStatus) manualImageUploadStatus.textContent = "Uploading image...";
        if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "flex";
        const formData = new FormData();
        formData.append("file", file);
        try {
            const res = await fetch("/api/upload", {
                method: "POST",
                body: formData,
            });
            const data = await res.json();
            if (data.ok && data.url) {
                if (manualImageUrlInput) manualImageUrlInput.value = data.url;
                if (manualImagePreview) manualImagePreview.src = data.url;
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "✓ Uploaded";
            } else {
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "✗ " + (data.error || "Upload failed");
            }
        } catch (err) {
            if (manualImageUploadStatus) manualImageUploadStatus.textContent = "✗ Network error";
        }
    }

    if (manualTakePhotoBtn && manualCameraFileInput) {
        manualTakePhotoBtn.addEventListener("click", () => manualCameraFileInput.click());
    }
    if (manualChooseFileBtn && manualImageFileInput) {
        manualChooseFileBtn.addEventListener("click", () => manualImageFileInput.click());
    }

    if (manualCameraFileInput) {
        manualCameraFileInput.addEventListener("change", (e) => {
            const file = e.target.files && e.target.files[0];
            handleManualFileUpload(file);
        });
    }

    if (manualImageFileInput) {
        manualImageFileInput.addEventListener("change", (e) => {
            const file = e.target.files && e.target.files[0];
            handleManualFileUpload(file);
        });
    }

    if (manualImageUrlInput) {
        manualImageUrlInput.addEventListener("input", () => {
            const url = manualImageUrlInput.value.trim();
            if (url) {
                if (manualImagePreview) manualImagePreview.src = url;
                if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "flex";
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "";
            } else {
                if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "none";
                if (manualImagePreview) manualImagePreview.src = "";
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "";
            }
        });
    }

    if (manualImageRemoveBtn) {
        manualImageRemoveBtn.addEventListener("click", () => {
            if (manualCameraFileInput) manualCameraFileInput.value = "";
            if (manualImageFileInput) manualImageFileInput.value = "";
            if (manualImageUrlInput) manualImageUrlInput.value = "";
            if (manualImagePreview) manualImagePreview.src = "";
            if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "none";
            if (manualImageUploadStatus) manualImageUploadStatus.textContent = "";
        });
    }

    function openManualModal() {
        if (!manualModal) return;
        manualModal.classList.add("is-open");
        manualModal.setAttribute("aria-hidden", "false");
        if (manualCameraFileInput) manualCameraFileInput.value = "";
        if (manualImageFileInput) manualImageFileInput.value = "";
        if (manualImageUrlInput) manualImageUrlInput.value = "";
        if (manualImagePreview) manualImagePreview.src = "";
        if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "none";
        if (manualImageUploadStatus) manualImageUploadStatus.textContent = "";
        setTimeout(() => {
            const titleInput = document.getElementById("manual-title");
            if (titleInput) titleInput.focus();
        }, 50);
    }

    function closeManualModal() {
        if (!manualModal) return;
        manualModal.classList.remove("is-open");
        manualModal.setAttribute("aria-hidden", "true");
    }

    if (openManualBtn) openManualBtn.addEventListener("click", openManualModal);
    if (closeManualBtn) closeManualBtn.addEventListener("click", closeManualModal);
    if (cancelManualBtn) cancelManualBtn.addEventListener("click", closeManualModal);
    if (manualModal) {
        manualModal.addEventListener("click", (e) => {
            if (e.target === manualModal) closeManualModal();
        });
    }

    if (manualForm) {
        manualForm.addEventListener("submit", async (e) => {
            e.preventDefault();
            const submitBtn = document.getElementById("save-manual-listing-btn");
            if (submitBtn) submitBtn.disabled = true;

            const parts = [];
            if (manualPartsContainer) {
                manualPartsContainer.querySelectorAll(".manual-part-row").forEach(row => {
                    const desc = (row.querySelector(".manual-part-desc")?.value || "").trim();
                    const cost = parseFloat(row.querySelector(".manual-part-cost")?.value || 0);
                    if (desc) {
                        parts.push({ description: desc, cost: cost });
                    }
                });
            }

            const checkedPlatforms = [];
            const platformCbs = document.querySelectorAll("#manual-platforms-grid .manual-platform-cb:checked");
            platformCbs.forEach(cb => {
                if (cb.value) checkedPlatforms.push(cb.value);
            });
            if (checkedPlatforms.length === 0) {
                const fallbackPlatform = document.getElementById("manual-platform")?.value || "local";
                checkedPlatforms.push(fallbackPlatform);
            }

            const imgUrl = (manualImageUrlInput?.value || "").trim() || null;

            const payload = {
                title: document.getElementById("manual-title").value.trim(),
                price: parseFloat(document.getElementById("manual-price").value || 0),
                purchase_price: parseFloat(document.getElementById("manual-purchase-price")?.value || 0),
                parts: parts,
                quantity: parseInt(document.getElementById("manual-quantity").value || 1),
                platform: checkedPlatforms[0],
                platforms: checkedPlatforms,
                image_url: imgUrl,
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
                    if (manualPartsContainer) manualPartsContainer.innerHTML = "";
                    if (manualCameraFileInput) manualCameraFileInput.value = "";
                    if (manualImageFileInput) manualImageFileInput.value = "";
                    if (manualImageUrlInput) manualImageUrlInput.value = "";
                    if (manualImagePreview) manualImagePreview.src = "";
                    if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "none";
                    if (manualImageUploadStatus) manualImageUploadStatus.textContent = "";
                    document.querySelectorAll("#manual-platforms-grid .manual-platform-cb").forEach(cb => {
                        cb.checked = (cb.value === "local");
                    });
                    updateManualCalculations();
                    closeManualModal();
                    loadListings();
                    loadStats();
                } else {
                    alert("Could not save listing: " + (data.error || "Unknown error"));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            } finally {
                if (submitBtn) submitBtn.disabled = false;
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
    const inviteTeamSelect = document.getElementById("invite-team-select");
    const openMailtoBtn = document.getElementById("open-mailto-link-btn");
    const copyEmailTemplateBtn = document.getElementById("copy-email-template-btn");
    const emailInviteActions = document.getElementById("email-invite-actions");
    const createTeamBtn = document.getElementById("dashboard-create-team-btn");
    const newTeamNameInput = document.getElementById("dashboard-new-team-name");
    const createTeamStatus = document.getElementById("dashboard-create-team-status");

    function openTeamsModal() {
        if (!teamsModal) return;
        teamsModal.classList.add("is-open");
        teamsModal.setAttribute("aria-hidden", "false");
        loadTeams();
    }

    function closeTeamsModal() {
        if (!teamsModal) return;
        teamsModal.classList.remove("is-open");
        teamsModal.setAttribute("aria-hidden", "true");
    }

    if (openTeamsBtn) openTeamsBtn.addEventListener("click", openTeamsModal);
    if (closeTeamsBtn) closeTeamsBtn.addEventListener("click", closeTeamsModal);
    if (closeTeamsFooterBtn) closeTeamsFooterBtn.addEventListener("click", closeTeamsModal);
    if (teamsModal) {
        teamsModal.addEventListener("click", (e) => {
            if (e.target === teamsModal) closeTeamsModal();
        });
    }

    async function copyToClipboard(text, feedbackEl) {
        try {
            await navigator.clipboard.writeText(text);
        } catch (e) {
            const ta = document.createElement("textarea");
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
        }
        if (feedbackEl) {
            feedbackEl.style.display = "inline-block";
            setTimeout(() => { feedbackEl.style.display = "none"; }, 2500);
        }
    }

    if (copyInviteBtn) {
        copyInviteBtn.addEventListener("click", () => {
            const urlInput = document.getElementById("share-invite-url");
            if (urlInput && urlInput.value) {
                copyToClipboard(urlInput.value, copyFeedback);
            }
        });
    }

    async function handleAddMember() {
        const email = (inviteEmailInput?.value || "").trim();
        if (!email) return;
        const selectedTeamId = inviteTeamSelect?.value ? Number(inviteTeamSelect.value) : null;
        const teamId = selectedTeamId || state.team || (loadedTeams[0] ? loadedTeams[0].id : null);
        if (!teamId) {
            if (inviteEmailStatus) inviteEmailStatus.innerHTML = "<span class='flash flash--error flash--inline'>Create or select a team first.</span>";
            return;
        }
        if (sendInviteBtn) sendInviteBtn.disabled = true;
        if (inviteEmailStatus) inviteEmailStatus.innerHTML = "<span style='color:var(--text-muted);'>Processing invitation…</span>";

        try {
            const res = await fetch(`/api/teams/${teamId}/members`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email }),
            });
            const data = await res.json();
            if (data.ok) {
                const teamName = data.team_name || "the team";
                if (data.email_sent) {
                    if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--success flash--inline'>✓ Added &amp; sent email invite to ${escapeHtml(email)}!</span>`;
                } else {
                    if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--success flash--inline'>✓ Added ${escapeHtml(email)} to ${escapeHtml(teamName)}! Ready to share invite:</span>`;
                }

                if (emailInviteActions && openMailtoBtn && copyEmailTemplateBtn) {
                    emailInviteActions.style.display = "flex";
                    const subject = encodeURIComponent(data.invite_subject || `Invitation to join ${teamName}`);
                    const bodyText = data.invite_body || `Join our team on Marketplace Dashboard: ${data.invite_url}`;
                    openMailtoBtn.href = `mailto:${encodeURIComponent(email)}?subject=${subject}&body=${encodeURIComponent(bodyText)}`;

                    copyEmailTemplateBtn.onclick = () => {
                        copyToClipboard(bodyText, inviteEmailStatus);
                        if (inviteEmailStatus) {
                            inviteEmailStatus.innerHTML = `<span class='flash flash--success flash--inline'>✓ Copied invite message to clipboard!</span>`;
                        }
                    };
                }
                loadTeams();
            } else {
                if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(data.error || "Failed to add")}</span>`;
            }
        } catch (err) {
            if (inviteEmailStatus) inviteEmailStatus.innerHTML = `<span class='flash flash--error flash--inline'>✗ ${escapeHtml(err.message)}</span>`;
        } finally {
            if (sendInviteBtn) sendInviteBtn.disabled = false;
        }
    }

    if (sendInviteBtn) sendInviteBtn.addEventListener("click", handleAddMember);
    if (inviteEmailInput) {
        inviteEmailInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleAddMember();
            }
        });
    }

    async function handleCreateTeam() {
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
    }

    if (createTeamBtn) createTeamBtn.addEventListener("click", handleCreateTeam);
    if (newTeamNameInput) {
        newTeamNameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                handleCreateTeam();
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

function hasPlatform(listing, p) {
    if (Array.isArray(listing.platforms) && listing.platforms.length > 0) {
        return listing.platforms.includes(p);
    }
    return (listing.platform || '').toLowerCase() === p.toLowerCase();
}

function renderPlatformBadges(platforms, fallback) {
    const list = (Array.isArray(platforms) && platforms.length > 0) ? platforms : (fallback ? [fallback] : []);
    if (list.length === 0) return getPlatformIcon("local");
    return list.map(p => getPlatformIcon(p)).join("");
}

// Render a single listing card
function renderCard(listing) {
    const platformBadges = renderPlatformBadges(listing.platforms, listing.platform);
    const price = listing.price_raw || `$${((listing.price_cents || 0) / 100).toFixed(2)} CAD`;
    const image = listing.image_url || "/static/img/placeholder.svg";

    const hasCost = (listing.total_cost_cents || 0) > 0;
    const costText = hasCost ? `$${(listing.total_cost || 0).toFixed(2)} CAD` : null;
    const netProfit = listing.net_profit !== undefined ? listing.net_profit : (((listing.price_cents || 0) - (listing.total_cost_cents || 0)) / 100);
    const profitSign = netProfit > 0 ? "+" : "";
    const profitColor = netProfit >= 0 ? "var(--success)" : "var(--danger)";

    return `
        <div class="listing-card" data-id="${listing.id}" data-platform="${listing.platform}">
            <div class="card-platform-badge" style="display:flex;gap:4px;flex-wrap:wrap;max-width:85%;">${platformBadges}</div>
            <div class="card-image">
                <img src="${image}" alt="${escapeHtml(listing.title || 'Listing')}" loading="lazy" onerror="this.src='/static/img/placeholder.svg'">
            </div>
            <div class="card-body">
                <h3 class="card-title">${escapeHtml(listing.title || "Untitled")}</h3>
                <p class="card-price">${price}</p>
                ${hasCost ? `
                    <div style="font-size: 11.5px; color: var(--text-muted); margin-top: -4px; margin-bottom: 6px;">
                        <span>Cost: ${costText}</span> · <span style="color: ${profitColor}; font-weight: 500;">Net: ${profitSign}$${netProfit.toFixed(2)} CAD</span>
                    </div>
                ` : ""}
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
