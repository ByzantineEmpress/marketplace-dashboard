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
        missingCost: false,
        sort: "created_at",
        order: "desc",
        total: 0,
        pageSize: 50,
        metricsDays: "all",
        // Grouping selection mode (button-based; drag is a follow-up).
        groupingMode: false,
        selectedIds: new Set(),
    };

    let loadedListings = {};
    let loadedTeams = [];
    // Set true right after a drag-drop so the click that follows a mouse drag
    // does not also open the listing modal.
    let suppressCardClick = false;

    const grid = document.getElementById("listing-grid");
    const pageInfo = document.getElementById("page-info");
    const prevBtn = document.getElementById("prev-page");
    const nextBtn = document.getElementById("next-page");

    // Clicking into a numeric field selects the value that is already there, so a
    // new number can be typed straight over it instead of backspacing first.
    //
    // Delegated on focusin because the modal fields are re-rendered every open.
    // The select() made on focus is normally undone by the click's own mouseup,
    // which collapses the selection back to a caret, so that first mouseup is
    // suppressed. A second click (field already focused) is left alone, which
    // lets a deliberate click place the caret to edit one digit.
    let selectingNumberField = false;

    document.addEventListener("focusin", (e) => {
        const el = e.target;
        if (!el || el.tagName !== "INPUT" || el.readOnly || el.type !== "number") return;
        selectingNumberField = true;
        try { el.select(); } catch (_) { /* harmless if refused */ }
    });

    document.addEventListener("mouseup", (e) => {
        if (!selectingNumberField) return;
        selectingNumberField = false;
        const el = e.target;
        if (el && el.tagName === "INPUT" && el.type === "number") {
            e.preventDefault();
            try { el.select(); } catch (_) { /* harmless if refused */ }
        }
    });

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
        // Only sent when active, so the URL stays clean otherwise.
        if (state.missingCost) {
            params.set("missing_cost", "true");
        }

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

            // Collapse grouped listings into one card each. The backend sends
            // every member plus a group_summary; we keep one representative card
            // and attach the member list so the modal can show the whole group.
            const raw = data.listings || [];
            const grouped = new Map();
            const order = [];
            raw.forEach(item => {
                if (item.group_summary && item.group_summary.id != null) {
                    const gid = item.group_summary.id;
                    if (!grouped.has(gid)) {
                        grouped.set(gid, { members: [] });
                        order.push(grouped.get(gid));
                    }
                    grouped.get(gid).members.push(item);
                } else {
                    order.push(item);
                }
            });
            const displayListings = order.map(entry => {
                if (entry.members) {
                    const rep = entry.members.find(
                        m => m.id === entry.members[0].group_summary.representative_id
                    ) || entry.members[0];
                    return {
                        ...rep,
                        group_member_list: entry.members,
                    };
                }
                return entry;
            });

            // Cache for modal lookup (every member, not just representatives).
            loadedListings = {};
            raw.forEach(item => { loadedListings[item.id] = item; });

            if (!displayListings || displayListings.length === 0) {
                renderEmptyState();
            } else {
                grid.innerHTML = displayListings.map(listing => renderCard(listing)).join("");
                // Wire click events on cards
                grid.querySelectorAll(".listing-card").forEach(card => {
                    card.classList.add("listing-card--selectable");
                    if (state.groupingMode) {
                        card.classList.add("listing-card--grouping-mode");
                    }
                    const id = Number(card.dataset.id);
                    if (state.selectedIds.has(id)) {
                        card.classList.add("listing-card--selected");
                    }
                    card.addEventListener("click", () => {
                        if (suppressCardClick) {
                            suppressCardClick = false;
                            return;
                        }
                        if (state.groupingMode) {
                            toggleGroupSelection(id, card);
                            return;
                        }
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
        const isFiltering = state.search || state.platform || state.status || state.team || state.missingCost;
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
                    state.missingCost = false;
                    state.page = 1;
                    document.getElementById("search-input").value = "";
                    document.getElementById("filter-platform").value = "";
                    document.getElementById("filter-status").value = "";
                    const teamSel = document.getElementById("filter-team");
                    if (teamSel) teamSel.value = "";
                    // Reset the missing-cost control too, or the filter would
                    // stay applied while its control looked cleared.
                    const costEl = document.getElementById("filter-missing-cost");
                    if (costEl) costEl.checked = false;
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
                    <p class="empty-state-text">Connect your own eBay or Etsy seller account to automatically sync your active listings.</p>
                    <a href="/marketplace-settings" class="btn btn--primary">Connect Your Accounts</a>
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

            // Render sales & profit timeline chart
            if (stats.timeline) {
                renderMetricsChart(stats.timeline, cur, sym);
            }
        } catch (e) { /* ignore */ }
    }

    // ====================================================================
    //  Timeline Chart Renderer (Native SVG, Zero CDN, 100% CSP compliant)
    // ====================================================================
    function renderMetricsChart(timeline, cur = "CAD", sym = "$") {
        const svg = document.getElementById("metrics-chart-svg");
        const emptyState = document.getElementById("chart-empty-state");
        const subtitle = document.getElementById("chart-period-subtitle");
        const tooltip = document.getElementById("chart-tooltip");
        const wrapper = document.getElementById("metrics-chart-wrapper");
        if (!svg) return;

        // Subtitle text based on state.metricsDays
        if (subtitle) {
            if (state.metricsDays === "7") subtitle.textContent = "(Last 7 Days)";
            else if (state.metricsDays === "30") subtitle.textContent = "(Last 30 Days)";
            else subtitle.textContent = "(All Time)";
        }

        const points = (timeline && Array.isArray(timeline.points)) ? timeline.points : [];
        const hasSales = points.some(p => (p.sold_count > 0 || p.revenue_cents > 0));

        if (emptyState) {
            emptyState.style.display = hasSales ? "none" : "flex";
        }

        // Setup dimensions
        const W = 800;
        const H = 220;
        const padL = 60;
        const padR = 25;
        const padT = 20;
        const padB = 35;
        const cW = W - padL - padR;
        const cH = H - padT - padB;

        if (points.length === 0) {
            svg.innerHTML = "";
            return;
        }

        // Determine value scales
        let maxVal = 0;
        let minVal = 0;
        points.forEach(p => {
            const rev = p.revenue || 0;
            const prof = p.profit || 0;
            if (rev > maxVal) maxVal = rev;
            if (prof > maxVal) maxVal = prof;
            if (prof < minVal) minVal = prof;
        });

        if (maxVal <= 0) maxVal = 100;
        const magnitude = Math.pow(10, Math.max(1, Math.floor(Math.log10(maxVal))));
        const normalized = maxVal / magnitude;
        let niceMultiplier = 1;
        if (normalized <= 1) niceMultiplier = 1;
        else if (normalized <= 2) niceMultiplier = 2;
        else if (normalized <= 5) niceMultiplier = 5;
        else niceMultiplier = 10;
        const niceMax = niceMultiplier * magnitude;
        const niceMin = minVal < 0 ? -niceMax * 0.25 : 0;
        const valRange = niceMax - niceMin;

        function getX(i) {
            if (points.length === 1) return padL + cW / 2;
            return padL + (i / (points.length - 1)) * cW;
        }

        function getY(val) {
            const ratio = (val - niceMin) / (valRange || 1);
            return padT + cH * (1 - ratio);
        }

        const zeroY = getY(0);

        // Generate Grid Lines and Y-Axis labels (4 steps: 0, 33%, 66%, 100%)
        let gridHtml = `
            <defs>
                <linearGradient id="chartRevGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="#3b82f6" stop-opacity="0.32"/>
                    <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.01"/>
                </linearGradient>
            </defs>
        `;

        const steps = 4;
        for (let s = 0; s <= steps; s++) {
            const val = niceMin + (valRange * s / steps);
            const y = getY(val);
            const labelStr = `${sym}${Math.round(val)}`;
            gridHtml += `
                <line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-width="1" stroke-dasharray="3 3"/>
                <text x="${padL - 8}" y="${(y + 4).toFixed(1)}" fill="var(--text-muted)" font-size="11" font-weight="500" text-anchor="end">${labelStr}</text>
            `;
        }

        // X-Axis labels
        let axisHtml = "";
        const n = points.length;
        let labelStep = 1;
        if (n > 14) labelStep = 5;
        else if (n > 8) labelStep = 2;

        points.forEach((p, i) => {
            if (i % labelStep === 0 || i === n - 1) {
                const x = getX(i);
                axisHtml += `
                    <text x="${x.toFixed(1)}" y="${(H - 12)}" fill="var(--text-muted)" font-size="11" font-weight="500" text-anchor="middle">${escapeHtml(p.label || '')}</text>
                `;
            }
        });

        // Revenue Area & Line
        const revPoints = points.map((p, i) => `${getX(i).toFixed(1)},${getY(p.revenue || 0).toFixed(1)}`);
        let revAreaPath = "";
        if (points.length > 0) {
            const firstX = getX(0).toFixed(1);
            const lastX = getX(points.length - 1).toFixed(1);
            revAreaPath = `M ${firstX},${zeroY.toFixed(1)} L ` + revPoints.join(" L ") + ` L ${lastX},${zeroY.toFixed(1)} Z`;
        }
        const revLinePath = "M " + revPoints.join(" L ");

        // Profit Line
        const profPoints = points.map((p, i) => `${getX(i).toFixed(1)},${getY(p.profit || 0).toFixed(1)}`);
        const profLinePath = "M " + profPoints.join(" L ");

        // Dots & Interactive Hover columns
        let dotsHtml = "";
        let hoverBands = "";
        const bandWidth = points.length > 1 ? (cW / (points.length - 1)) : cW;

        points.forEach((p, i) => {
            const x = getX(i);
            const yRev = getY(p.revenue || 0);
            const yProf = getY(p.profit || 0);

            if (p.revenue > 0 || p.profit !== 0) {
                dotsHtml += `
                    <circle cx="${x.toFixed(1)}" cy="${yRev.toFixed(1)}" r="3.5" fill="#3b82f6" stroke="var(--bg-surface)" stroke-width="1.5"/>
                    <circle cx="${x.toFixed(1)}" cy="${yProf.toFixed(1)}" r="3.5" fill="#10b981" stroke="var(--bg-surface)" stroke-width="1.5"/>
                `;
            }

            const rx = x - bandWidth / 2;
            hoverBands += `
                <rect class="chart-hover-band" data-idx="${i}" x="${rx.toFixed(1)}" y="${padT}" width="${bandWidth.toFixed(1)}" height="${cH.toFixed(1)}" fill="transparent" style="cursor: pointer;"/>
            `;
        });

        // Guidelines & active indicator points
        const guideHtml = `<line id="chart-guideline" x1="0" y1="${padT}" x2="0" y2="${padT + cH}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="2 2" opacity="0"/>`;
        const activeRevDot = `<circle id="chart-active-rev-dot" cx="0" cy="0" r="5.5" fill="#3b82f6" stroke="#fff" stroke-width="2" opacity="0"/>`;
        const activeProfDot = `<circle id="chart-active-prof-dot" cx="0" cy="0" r="5.5" fill="#10b981" stroke="#fff" stroke-width="2" opacity="0"/>`;

        svg.innerHTML = `
            ${gridHtml}
            ${axisHtml}
            <path d="${revAreaPath}" fill="url(#chartRevGrad)" />
            <path d="${revLinePath}" fill="none" stroke="#3b82f6" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
            <path d="${profLinePath}" fill="none" stroke="#10b981" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            ${dotsHtml}
            ${guideHtml}
            ${activeRevDot}
            ${activeProfDot}
            ${hoverBands}
        `;

        // Tooltip handlers
        const bands = svg.querySelectorAll(".chart-hover-band");
        const guide = svg.querySelector("#chart-guideline");
        const dotR = svg.querySelector("#chart-active-rev-dot");
        const dotP = svg.querySelector("#chart-active-prof-dot");

        function showTooltip(idx) {
            const p = points[idx];
            if (!p || !tooltip) return;

            const x = getX(idx);
            const yRev = getY(p.revenue || 0);
            const yProf = getY(p.profit || 0);

            if (guide) {
                guide.setAttribute("x1", x.toFixed(1));
                guide.setAttribute("x2", x.toFixed(1));
                guide.setAttribute("opacity", "0.7");
            }
            if (dotR) {
                dotR.setAttribute("cx", x.toFixed(1));
                dotR.setAttribute("cy", yRev.toFixed(1));
                dotR.setAttribute("opacity", "1");
            }
            if (dotP) {
                dotP.setAttribute("cx", x.toFixed(1));
                dotP.setAttribute("cy", yProf.toFixed(1));
                dotP.setAttribute("opacity", "1");
            }

            const profPrefix = (p.profit || 0) >= 0 ? "+" : "";
            const profColor = (p.profit || 0) >= 0 ? "var(--success)" : "var(--danger)";
            const countStr = `${p.sold_count || 0} ${p.sold_count === 1 ? 'sale' : 'sales'}`;

            tooltip.innerHTML = `
                <div class="chart-tooltip-date">${escapeHtml(p.date || p.label)}</div>
                <div class="chart-tooltip-row">
                    <span class="chart-tooltip-label" style="color:#3b82f6;">● Revenue:</span>
                    <span class="chart-tooltip-val">${sym}${(p.revenue || 0).toFixed(2)} ${cur}</span>
                </div>
                <div class="chart-tooltip-row">
                    <span class="chart-tooltip-label" style="color:#10b981;">● Realized Profit:</span>
                    <span class="chart-tooltip-val" style="color:${profColor};">${profPrefix}${sym}${(p.profit || 0).toFixed(2)} ${cur}</span>
                </div>
                <div class="chart-tooltip-row">
                    <span class="chart-tooltip-label">Volume:</span>
                    <span class="chart-tooltip-val">${countStr}</span>
                </div>
            `;

            if (wrapper) {
                const rect = wrapper.getBoundingClientRect();
                const posX = (x / W) * rect.width;
                const topTarget = (Math.min(yRev, yProf) / H) * rect.height;
                tooltip.style.left = `${posX}px`;
                tooltip.style.top = `${Math.max(topTarget - 12, 10)}px`;
                tooltip.style.display = "block";
            }
        }

        function hideTooltip() {
            if (tooltip) tooltip.style.display = "none";
            if (guide) guide.setAttribute("opacity", "0");
            if (dotR) dotR.setAttribute("opacity", "0");
            if (dotP) dotP.setAttribute("opacity", "0");
        }

        bands.forEach(b => {
            const idx = parseInt(b.getAttribute("data-idx"), 10);
            b.addEventListener("mouseenter", () => showTooltip(idx));
            b.addEventListener("mousemove", () => showTooltip(idx));
            b.addEventListener("mouseleave", hideTooltip);
            b.addEventListener("touchstart", (e) => {
                showTooltip(idx);
            }, { passive: true });
        });

        if (wrapper) {
            wrapper.addEventListener("mouseleave", hideTooltip);
        }
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
                    <span class="card-status card-status--${listing.status || 'unknown'}">${listing.status === 'written_off' ? 'Written Off' : (listing.status || 'unknown')}</span>
                    ${listing.is_sold ? '<span class="card-status card-status--sold">Sold</span>' : ''}
                </div>
            </div>

            ${renderGroupSection(listing)}

            <!-- Multi-Channel Cross-Listing Section -->
            <div class="modal-platforms-section" style="margin-bottom: 14px; padding: 12px 14px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <h4 style="margin: 0; font-size: 13.5px; font-weight: 600;">🏷️ Listed On (Channels &amp; Marketplaces)</h4>
                    <span style="font-size: 11.5px; color: var(--text-muted);">Mark all places where this is active</span>
                </div>
                <div class="platforms-grid detail-platforms-grid">
                    ${renderPlatformChips(listing, "detail-platform-cb", "")}
                </div>
            </div>

            <div class="modal-details-grid">
                <div class="modal-detail-item">
                    <span class="modal-detail-label">Quantity</span>
                    <span class="modal-detail-value">${listing.available_quantity ?? '—'}</span>
                </div>
                ${renderEngagementMetrics(listing)}
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
                        <label class="cost-free-toggle" title="Records a deliberate $0 cost, so this listing stops counting as 'cost missing'">
                            <input type="checkbox" class="detail-cost-free-cb" ${listing.cost_is_free ? "checked" : ""}>
                            <span>Got it free</span>
                        </label>
                    </div>
                    <div class="form-group" style="flex: 1; margin-bottom: 0;">
                        <label style="font-size: 12px; font-weight: 600; display: block; margin-bottom: 4px;">Selling / Listed Price ($ CAD)</label>
                        <input type="number" step="0.01" min="0" class="detail-selling-price-input" value="${((listing.price_cents || 0) / 100).toFixed(2)}" style="width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);">
                    </div>
                    <div class="form-group" style="flex: 1; margin-bottom: 0;">
                        <label style="font-size: 12px; font-weight: 600; display: block; margin-bottom: 4px;">Postage You Paid ($ CAD)</label>
                        <input type="number" step="0.01" min="0" class="detail-shipping-cost-input" value="${((listing.shipping_cost_cents || 0) / 100).toFixed(2)}" title="What you paid to ship it. Marketplace fees and the buyer's postage are already netted off the payout." style="width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-card); color: var(--text);">
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
                <div class="detail-financial-summary" style="padding: 8px 10px; background: var(--bg-elevated); border: 1px dashed var(--border); border-radius: 6px; font-size: 12px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;">
                    <span>Total Investment: <strong class="detail-total-cost-val">$0.00 CAD</strong></span>
                    <span><span class="detail-profit-label">Est. Net Profit</span>: <strong class="detail-net-profit-val" style="color: var(--success);">+$0.00 CAD</strong> <span class="detail-margin-val" style="color: var(--text-muted);">(0%)</span></span>
                </div>
                ${renderSaleEconomics(listing)}
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
        const shippingInput = modalBody.querySelector(".detail-shipping-cost-input");
        const totalCostVal = modalBody.querySelector(".detail-total-cost-val");
        const netProfitVal = modalBody.querySelector(".detail-net-profit-val");
        const profitLabel = modalBody.querySelector(".detail-profit-label");
        const marginVal = modalBody.querySelector(".detail-margin-val");
        const saveCostBtn = modalBody.querySelector(".detail-save-cost-btn");
        const saveCostStatus = modalBody.querySelector(".detail-save-cost-status");
        const costFreeCb = modalBody.querySelector(".detail-cost-free-cb");

        // Wire the group's Ungroup buttons (one per member row).
        modalBody.querySelectorAll(".group-ungroup-btn").forEach(btn => {
            btn.addEventListener("click", async (e) => {
                e.stopPropagation();
                const memberId = Number(btn.dataset.id);
                btn.disabled = true;
                btn.textContent = "Removing…";
                try {
                    const res = await fetch(`/api/listings/${memberId}/ungroup`, {
                        method: "POST",
                    });
                    const data = await res.json();
                    if (!res.ok || !data.ok) {
                        throw new Error(data.error || "Ungroup failed");
                    }
                    closeModal();
                    loadListings();
                    loadStats();
                } catch (err) {
                    btn.disabled = false;
                    btn.textContent = "Ungroup";
                    alert(`Could not ungroup: ${err.message}`);
                }
            });
        });

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
                // Guard the size here so an oversized photo gives a useful
                // message instead of a 413 after a slow mobile upload.
                const MAX_BYTES = 15 * 1024 * 1024;
                if (file.size > MAX_BYTES) {
                    alert(`That photo is ${(file.size / 1048576).toFixed(1)} MB — the limit is 15 MB.\n\n` +
                          `Most phones can be set to a smaller photo size, or try the Gallery picker.`);
                    return;
                }
                const formData = new FormData();
                formData.append("file", file);
                const res = await fetch("/api/upload", { method: "POST", body: formData });
                // A 500 (or any non-JSON response) used to fall through to
                // res.json(), throw a SyntaxError, and surface as a bare
                // "network error" — which hid the real cause.
                let data = null;
                try {
                    data = await res.json();
                } catch (_) {
                    data = null;
                }
                if (data && data.ok) {
                    if (detailImgUrlInput) detailImgUrlInput.value = data.url;
                    if (modalImg) modalImg.src = data.url;
                } else if (data && data.error) {
                    alert("Upload failed: " + data.error);
                } else {
                    alert(`Upload failed (HTTP ${res.status}). ` +
                          `Check the server log if this keeps happening.`);
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
            // Postage the seller paid belongs in the investment total: it is money
            // out of pocket on this item, exactly like a part.
            const shippingCost = parseFloat(shippingInput?.value || 0);
            const totalCost = purchasePrice + partsTotal + shippingCost;
            const sellingPrice = parseFloat(sellingInput?.value || 0);

            // Once an item has SOLD, the listed price is not its revenue: the
            // marketplace has already paid out, net of its fee, and that payout is
            // what profit has to be measured against. Showing a list-price estimate
            // next to the payout gave two different answers for the same sale.
            const payout = (listing.net_payout_cents || 0) / 100;
            const sold = payout > 0;
            const revenue = sold ? payout : sellingPrice;

            const netProfit = revenue - totalCost;
            const margin = revenue > 0 ? ((netProfit / revenue) * 100).toFixed(1) : "0.0";

            if (totalCostVal) totalCostVal.textContent = `$${totalCost.toFixed(2)} CAD`;
            if (profitLabel) {
                profitLabel.textContent = sold ? "Actual Profit / Loss" : "Est. Net Profit";
            }
            if (netProfitVal) {
                const sign = netProfit > 0 ? "+" : (netProfit < 0 ? "−" : "");
                netProfitVal.textContent = `${sign}$${Math.abs(netProfit).toFixed(2)} CAD`;
                netProfitVal.style.color = netProfit >= 0 ? "var(--success)" : "var(--danger)";
            }
            if (marginVal) {
                marginVal.textContent = `(${margin}%)`;
            }
        }

        updateDetailCalculations();

        // "Got it free" records a deliberate $0 cost. The amount field is zeroed
        // and disabled so the two can never contradict each other; the profit
        // figures then update against a known-zero cost.
        function applyCostFreeState() {
            const free = !!(costFreeCb && costFreeCb.checked);
            if (purchaseInput) {
                if (free) purchaseInput.value = "0.00";
                purchaseInput.disabled = free;
            }
            updateDetailCalculations();
        }
        if (costFreeCb) {
            costFreeCb.addEventListener("change", applyCostFreeState);
            applyCostFreeState();
        }

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
                const shippingCost = parseFloat(shippingInput?.value || 0);

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
                            cost_is_free: !!(costFreeCb && costFreeCb.checked),
                            shipping_cost: shippingCost,
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

                        // Refresh the card in the grid, then close the modal.
                        // Saving is a "done" action, so leaving the modal open
                        // just meant closing it by hand every time; the updated
                        // card behind it is the confirmation.
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
                        closeModal();
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
        const writeOffBtn = document.getElementById("modal-write-off-btn");
        const restoreBtn = document.getElementById("modal-restore-btn");
        const deleteBtn = document.getElementById("modal-delete-btn");

        if (teamSelect) {
            teamSelect.innerHTML = teamOptions;
        }
        if (statusSpan) {
            statusSpan.textContent = "";
        }

        const isWrittenOff = listing.status === "written_off";
        const isSold = Boolean(listing.is_sold);

        if (markSoldBtn) {
            markSoldBtn.style.display = (isSold || isWrittenOff) ? "none" : "";
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

        if (writeOffBtn) {
            writeOffBtn.style.display = (isSold || isWrittenOff) ? "none" : "";
            writeOffBtn.onclick = async () => {
                const titleStr = listing.title || "this item";
                if (!confirm(`Write off "${titleStr}" as an inventory loss?\n\nThis marks it as unsellable and removes it from active inventory.`)) return;
                try {
                    const res = await fetch(`/api/listings/${listing.id}/write-off`, { method: "POST" });
                    const resData = await res.json();
                    if (resData.ok) {
                        listing.status = "written_off";
                        listing.is_sold = false;
                        closeModal();
                        loadListings();
                        loadStats();
                    } else {
                        alert("Could not write off listing: " + (resData.error || "Unknown error"));
                    }
                } catch (err) {
                    alert("Could not write off listing: " + err.message);
                }
            };
        }

        if (restoreBtn) {
            restoreBtn.style.display = isWrittenOff ? "" : "none";
            restoreBtn.onclick = async () => {
                const titleStr = listing.title || "this item";
                if (!confirm(`Restore "${titleStr}" back to active inventory?`)) return;
                try {
                    const res = await fetch(`/api/listings/${listing.id}/restore`, { method: "POST" });
                    const resData = await res.json();
                    if (resData.ok) {
                        listing.status = "active";
                        listing.is_sold = false;
                        closeModal();
                        loadListings();
                        loadStats();
                    } else {
                        alert("Could not restore listing: " + (resData.error || "Unknown error"));
                    }
                } catch (err) {
                    alert("Could not restore listing: " + err.message);
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

    // Enter in any editor field performs the same save as the Save button (which
    // closes the modal). Registered once, here, rather than inside
    // openListingModal: the handler is delegated, and openListingModal runs on
    // every open, so registering it there would stack duplicate listeners and
    // fire the save once per previous open.
    //
    // Textareas keep Enter for newlines, buttons keep their own Enter
    // activation, and an in-progress or IME-composing keypress is ignored.
    if (modalBody) {
        modalBody.addEventListener("keydown", (e) => {
            if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
            const el = e.target;
            if (!el) return;
            if (el.tagName !== "INPUT" && el.tagName !== "SELECT") return;
            const saveBtn = modalBody.querySelector(".detail-save-cost-btn");
            if (!saveBtn || saveBtn.disabled) return;
            e.preventDefault();
            saveBtn.click();
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
        const MAX_BYTES = 15 * 1024 * 1024;
        if (file.size > MAX_BYTES) {
            if (manualImageUploadStatus) {
                manualImageUploadStatus.textContent =
                    `✗ Photo is ${(file.size / 1048576).toFixed(1)} MB, limit is 15 MB`;
            }
            return;
        }
        if (manualImageUploadStatus) manualImageUploadStatus.textContent = "Uploading image...";
        if (manualImagePreviewWrap) manualImagePreviewWrap.style.display = "flex";
        const formData = new FormData();
        formData.append("file", file);
        try {
            const res = await fetch("/api/upload", {
                method: "POST",
                body: formData,
            });
            // Split parsing from the request so a non-JSON error body reports
            // the HTTP status instead of a bare "Network error". A 500 there
            // was previously indistinguishable from the phone losing signal.
            let data = null;
            try {
                data = await res.json();
            } catch (_) {
                data = null;
            }
            if (data && data.ok && data.url) {
                if (manualImageUrlInput) manualImageUrlInput.value = data.url;
                if (manualImagePreview) manualImagePreview.src = data.url;
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "✓ Uploaded";
            } else if (data && data.error) {
                if (manualImageUploadStatus) manualImageUploadStatus.textContent = "✗ " + data.error;
            } else {
                if (manualImageUploadStatus) {
                    manualImageUploadStatus.textContent =
                        `✗ Upload failed (HTTP ${res.status})`;
                }
            }
        } catch (err) {
            if (manualImageUploadStatus) {
                manualImageUploadStatus.textContent = "✗ " + (err.message || "Request failed");
            }
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

    // Event: "missing cost" filter
    const missingCostEl = document.getElementById("filter-missing-cost");
    if (missingCostEl) {
        missingCostEl.addEventListener("change", (e) => {
            state.missingCost = e.target.checked;
            state.page = 1;
            loadListings();
        });
    }

    // -- Grouping selection mode (button-based; drag-and-drop is a follow-up) --

    const groupModeBtn = document.getElementById("group-mode-btn");
    const groupActionBar = document.getElementById("group-action-bar");
    const groupSelectionCount = document.getElementById("group-selection-count");
    const groupConfirmBtn = document.getElementById("group-confirm-btn");
    const groupCancelBtn = document.getElementById("group-cancel-btn");

    function setGroupingMode(on) {
        state.groupingMode = !!on;
        if (!on) {
            state.selectedIds.clear();
        }
        updateGroupingUI();
        loadListings();
    }

    function updateGroupingUI() {
        const count = state.selectedIds.size;
        if (groupActionBar) {
            groupActionBar.style.display = state.groupingMode ? "flex" : "none";
        }
        if (groupModeBtn) {
            groupModeBtn.textContent = state.groupingMode ? "✕ Exit Group Mode" : "🔗 Group Listings";
            groupModeBtn.classList.toggle("is-active", state.groupingMode);
        }
        if (groupSelectionCount) {
            groupSelectionCount.textContent = `${count} selected`;
        }
        if (groupConfirmBtn) {
            groupConfirmBtn.disabled = count < 2;
        }
    }

    function toggleGroupSelection(id, card) {
        if (state.selectedIds.has(id)) {
            state.selectedIds.delete(id);
        } else {
            state.selectedIds.add(id);
        }
        if (card) {
            card.classList.toggle("listing-card--selected", state.selectedIds.has(id));
        }
        updateGroupingUI();
    }

    if (groupModeBtn) {
        groupModeBtn.addEventListener("click", () => setGroupingMode(!state.groupingMode));
    }
    if (groupCancelBtn) {
        groupCancelBtn.addEventListener("click", () => setGroupingMode(false));
    }

    // Shared: group a set of listing ids, returning true on success. Used by the
    // button flow and by drag-and-drop so the request logic lives in one place.
    async function groupListingsByIds(ids) {
        try {
            const res = await fetch("/api/listings/group", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ listing_ids: ids }),
            });
            const data = await res.json();
            if (!res.ok || !data.ok) {
                throw new Error(data.error || "Grouping failed");
            }
            return true;
        } catch (err) {
            alert("Could not group listings: " + err.message);
            return false;
        }
    }

    if (groupConfirmBtn) {
        groupConfirmBtn.addEventListener("click", async () => {
            const ids = [...state.selectedIds];
            if (ids.length < 2) return;
            groupConfirmBtn.disabled = true;
            const ok = await groupListingsByIds(ids);
            groupConfirmBtn.disabled = false;
            if (ok) {
                setGroupingMode(false);
                loadStats();
            }
        });
    }

    // Drag one listing card onto another to group them. Mouse drags start after a
    // small movement; touch requires a 400ms hold first so a normal scroll swipe
    // is not mistaken for a drag (touch-action: pan-y keeps vertical scroll).
    function initDragToGroup(gridEl) {
        let drag = null;

        function reset() {
            if (!drag) return;
            if (drag.longPressTimer) clearTimeout(drag.longPressTimer);
            if (drag.ghost && drag.ghost.parentNode) drag.ghost.parentNode.removeChild(drag.ghost);
            if (drag.sourceCard) drag.sourceCard.classList.remove("listing-card--dragging");
            if (drag.targetCard) drag.targetCard.classList.remove("listing-card--drop-target");
            drag = null;
        }

        function cardAt(x, y) {
            const el = document.elementFromPoint(x, y);
            return el && el.closest ? el.closest(".listing-card") : null;
        }

        gridEl.addEventListener("pointerdown", function (e) {
            if (state.groupingMode) return;              // button mode owns taps
            if (e.pointerType === "mouse" && e.button !== 0) return;
            const card = e.target.closest ? e.target.closest(".listing-card") : null;
            if (!card) return;

            drag = {
                pointerId: e.pointerId,
                sourceCard: card,
                sourceId: Number(card.dataset.id),
                startX: e.clientX,
                startY: e.clientY,
                armed: e.pointerType !== "touch",
                dragging: false,
                ghost: null,
                targetCard: null,
                longPressTimer: null,
            };

            if (e.pointerType === "touch") {
                drag.longPressTimer = setTimeout(function () {
                    if (drag) drag.armed = true;
                }, 400);
            }
        });

        function beginDrag(e) {
            if (!drag) return;
            drag.dragging = true;
            drag.sourceCard.classList.add("listing-card--dragging");

            // A mini-card ghost — thumbnail, title and price — so it is obvious
            // which item is being dragged, not just a bare text label.
            const ghost = document.createElement("div");
            ghost.className = "drag-ghost";

            const imgEl = drag.sourceCard.querySelector(".card-image img");
            if (imgEl && imgEl.src) {
                const img = document.createElement("img");
                img.className = "drag-ghost-img";
                img.src = imgEl.src;
                img.alt = "";
                ghost.appendChild(img);
            }

            const body = document.createElement("div");
            body.className = "drag-ghost-body";

            const titleEl = drag.sourceCard.querySelector(".card-title");
            const title = document.createElement("div");
            title.className = "drag-ghost-title";
            title.textContent = titleEl ? titleEl.textContent : "Listing";
            body.appendChild(title);

            const priceEl = drag.sourceCard.querySelector(".card-price");
            if (priceEl && priceEl.textContent) {
                const price = document.createElement("div");
                price.className = "drag-ghost-price";
                price.textContent = priceEl.textContent;
                body.appendChild(price);
            }

            ghost.appendChild(body);
            document.body.appendChild(ghost);
            drag.ghost = ghost;

            try { drag.sourceCard.setPointerCapture(e.pointerId); } catch (err) {}
        }

        function moveGhost(e) {
            if (drag && drag.ghost) {
                drag.ghost.style.left = (e.clientX + 14) + "px";
                drag.ghost.style.top = (e.clientY + 14) + "px";
            }
        }

        gridEl.addEventListener("pointermove", function (e) {
            if (!drag || e.pointerId !== drag.pointerId) return;
            const dist = Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY);

            if (!drag.dragging) {
                // A touch that moves before the hold elapses is a scroll, not a drag.
                if (e.pointerType === "touch" && !drag.armed && dist > 12) {
                    reset();
                    return;
                }
                if (drag.armed && dist > 8) beginDrag(e);
                if (!drag.dragging) return;
            }

            moveGhost(e);

            const card = cardAt(e.clientX, e.clientY);
            const target = card && card !== drag.sourceCard ? card : null;
            if (target !== drag.targetCard) {
                if (drag.targetCard) drag.targetCard.classList.remove("listing-card--drop-target");
                drag.targetCard = target;
                if (target) target.classList.add("listing-card--drop-target");
            }
        });

        function finish(e) {
            if (!drag || e.pointerId !== drag.pointerId) return;
            const sourceId = drag.sourceId;
            const targetId = drag.targetCard ? Number(drag.targetCard.dataset.id) : null;
            const wasDragging = drag.dragging;
            reset();
            if (wasDragging && targetId && targetId !== sourceId) {
                suppressCardClick = true;
                groupListingsByIds([sourceId, targetId]).then(function (ok) {
                    if (ok) {
                        loadListings();
                        loadStats();
                    }
                });
            }
        }

        gridEl.addEventListener("pointerup", finish);
        gridEl.addEventListener("pointercancel", reset);
        // Safety net: the native HTML5 image drag (which would otherwise hijack
        // a drag that starts on the thumbnail) is suppressed grid-wide.
        gridEl.addEventListener("dragstart", function (e) {
            e.preventDefault();
        });
    }

    initDragToGroup(grid);

    // Event: refresh button
    document.getElementById("refresh-btn").addEventListener("click", () => {
        loadListings();
        loadStats();
    });

    // Event: sync every connected marketplace at once.
    //
    // The server does the work; this only reports it. The button borrows its own
    // label as the progress indicator because a sync of two marketplaces can run
    // for tens of seconds, and a button that looks idle while the request is in
    // flight invites a second click that starts a second sync.
    const syncAllBtn = document.getElementById("sync-all-btn");
    if (syncAllBtn) {
        const idleLabel = syncAllBtn.textContent;
        let restoreTimer = null;

        const setLabel = (text, ms) => {
            syncAllBtn.textContent = text;
            if (restoreTimer) {
                clearTimeout(restoreTimer);
            }
            if (ms) {
                restoreTimer = setTimeout(() => {
                    syncAllBtn.textContent = idleLabel;
                }, ms);
            }
        };

        syncAllBtn.addEventListener("click", async () => {
            if (syncAllBtn.disabled) {
                return; // One run at a time.
            }
            syncAllBtn.disabled = true;
            setLabel("Syncing…");

            try {
                const res = await fetch("/api/accounts/sync-all", { method: "POST" });
                const data = await res.json().catch(() => ({}));

                if (!res.ok) {
                    setLabel("Sync failed", 6000);
                    console.warn("Sync all failed:", data.error || res.status);
                } else {
                    const results = data.results || {};
                    const added = Object.values(results).reduce(
                        (sum, r) => sum + (r.listings_added || 0), 0);
                    const sales = Object.values(results).reduce(
                        (sum, r) => sum + ((r.sales || {}).created || 0), 0);
                    const failed = (data.failed || []).length;

                    if (failed) {
                        // Name the failures rather than reporting a bare success:
                        // a partial sync that hides which part broke is worse than
                        // no report at all.
                        setLabel(`${failed} of ${data.synced.length} failed`, 8000);
                        console.warn("Sync all partial:", data.results);
                    } else {
                        const parts = [];
                        if (added) parts.push(`${added} new`);
                        if (sales) parts.push(`${sales} sold`);
                        setLabel(parts.length ? `Synced: ${parts.join(", ")}` : "Up to date", 5000);
                    }
                }

                // Whatever happened, show what is actually stored now.
                loadListings();
                loadStats();
            } catch (e) {
                setLabel("Sync failed", 6000);
                console.warn("Sync all errored:", e);
            } finally {
                syncAllBtn.disabled = false;
            }
        });
    }

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

// Per-platform engagement metrics. eBay has watchers (Trading API WatchCount)
// and no reliable view count; Etsy has views and favorites. Other platforms keep
// the generic "Views" label.
function renderEngagementMetrics(listing) {
    const p = (listing.platform || '').toLowerCase();
    if (p === 'etsy') {
        return `
            <div class="modal-detail-item">
                <span class="modal-detail-label">Views</span>
                <span class="modal-detail-value">${listing.views_count ?? 0}</span>
            </div>
            <div class="modal-detail-item">
                <span class="modal-detail-label">Favorites</span>
                <span class="modal-detail-value">${listing.favorites_count ?? 0}</span>
            </div>`;
    }
    if (p === 'ebay') {
        return `
            <div class="modal-detail-item">
                <span class="modal-detail-label">Watchers</span>
                <span class="modal-detail-value">${listing.watchers_count ?? 0}</span>
            </div>`;
    }
    return `
        <div class="modal-detail-item">
            <span class="modal-detail-label">Views</span>
            <span class="modal-detail-value">${listing.views_count ?? 0}</span>
        </div>`;
}

// Marketplace economics for a completed sale. The listed price is not revenue:
// the marketplace takes a fee, and the buyer's postage was never the seller's to
// keep. The payout is the only honest basis for profit, so it is shown alongside
// the numbers that produced it.
//
// The deduction line matters as much as the total. Showing the payout and then a
// final figure, with no visible subtraction, reads as though cost of goods and
// postage were never taken off — and the fees are buried inside the payout, so
// they need naming too.
function renderSaleEconomics(listing) {
    const payout = listing.net_payout_cents || 0;
    if (!payout) return "";
    const money = (cents) => `$${((cents || 0) / 100).toFixed(2)}`;
    const fees = listing.fees_cents || 0;
    const charged = listing.shipping_charged_cents || 0;
    const cogs = (listing.purchase_price_cents || 0) + (listing.parts_cost_cents || 0);
    const postage = listing.shipping_cost_cents || 0;
    const profit = payout - cogs - postage;
    const color = profit >= 0 ? "var(--success)" : "var(--danger)";
    return `
        <div class="detail-sale-economics">
            <div class="detail-sale-line">
                <span>Sale ${money(listing.price_cents)} + postage ${money(charged)}</span>
                <span>Marketplace fees −${money(fees)}</span>
                <span>= Payout <strong>${money(payout)}</strong></span>
            </div>
            <div class="detail-sale-line">
                <span>− Cost of goods ${money(cogs)}</span>
                <span>− Postage you paid ${money(postage)}</span>
                <span>= ${profit >= 0 ? "Actual profit" : "Actual loss"}
                    <strong style="color:${color}">${profit >= 0 ? "+" : "−"}${money(Math.abs(profit))}</strong></span>
            </div>
        </div>`;
}

// Engagement stats for a card, as labelled rectangular boxes so they stand out
// from the rounded status/alerts. eBay has views + watchers; Etsy has views +
// favorites. Always shown, including while the value is still zero.
function renderEngagementChip(listing) {
    const p = (listing.platform || '').toLowerCase();
    const box = (label, value) =>
        `<span class="card-metric"><span class="card-metric-label">${label}</span>` +
        `<span class="card-metric-value">${value ?? 0}</span></span>`;

    if (p === 'ebay') {
        return box("Views", listing.views_count) + box("Watchers", listing.watchers_count);
    }
    if (p === 'etsy') {
        return box("Views", listing.views_count) + box("Favorites", listing.favorites_count);
    }
    return '';
}

// Consistent platform selector chips. The previous version was a grid of bare
// checkboxes with differently-sized coloured labels, which read as scrambled.
// Each chip has the platform name, a stable checkmark, and a highlighted
// "checked" state via :has(input:checked).
function renderPlatformChips(listing, checkboxClass, nameAttr) {
    const options = [
        ["ebay", "eBay", "#e53238"],
        ["etsy", "Etsy", "#F56400"],
        ["facebook", "FB Marketplace", "#1877F2"],
        ["local", "Local / In-Person", "#10b981"],
        ["poshmark", "Poshmark", "#8E1A34"],
        ["amazon", "Amazon", "#FF9900"],
        ["craigslist", "Craigslist / Kijiji", "#795548"],
    ];
    const name = nameAttr ? `name="${nameAttr}"` : "";
    return options.map(([value, label, color]) => {
        const checked = hasPlatform(listing, value) ? "checked" : "";
        return `
            <label class="platform-chip">
                <input type="checkbox" class="${checkboxClass}" value="${value}" ${name} ${checked}>
                <span class="platform-chip-label" style="color:${color}">${label}</span>
                <span class="platform-chip-check">✓</span>
            </label>`;
    }).join("");
}

// The grouped-listing section inside the detail modal. Shows every channel the
// item is listed on, plus an Ungroup control; a channel sold while others are
// live becomes a "delist elsewhere" alert.
function renderGroupSection(listing) {
    const summary = listing.group_summary;
    if (!summary || !summary.members || summary.members.length < 2) return "";

    const memberRows = summary.members.map(m => {
        const badges = renderPlatformBadges([m.platform], m.platform);
        const status = m.is_sold || (m.status === "sold")
            ? `<span class="card-status card-status--sold">Sold</span>`
            : `<span class="card-status card-status--active">Active</span>`;
        return `<div class="group-member-row">
            ${badges}
            <span class="group-member-title">${escapeHtml(m.title || "Untitled")}</span>
            ${status}
            <button type="button" class="btn btn--sm btn--outline group-ungroup-btn" data-id="${m.id}">Ungroup</button>
        </div>`;
    }).join("");

    const alert = summary.needs_delist
        ? `<div class="card-delist-alert" style="margin-bottom: 8px;">Sold on ${escapeHtml((summary.sold_platforms || []).join(", "))} — delist from ${escapeHtml((summary.active_platforms || []).join(", "))}</div>`
        : "";

    return `
        <div class="modal-group-section" style="margin-bottom: 14px; padding: 12px 14px; background: var(--bg); border: 1px solid var(--accent); border-radius: var(--radius);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <h4 style="margin:0;font-size:13.5px;font-weight:600;">🔗 Grouped Item · ${summary.members.length} channels</h4>
                <span style="font-size:11.5px;color:var(--text-muted);">One unit, listed ${summary.members.length} places</span>
            </div>
            ${alert}
            ${memberRows}
            <div style="font-size:11.5px;color:var(--text-muted);margin-top:8px;">
                Shared qty: ${summary.available_quantity ?? 1} ·
                Shared cost: $${((summary.total_cost_cents || 0) / 100).toFixed(2)} CAD
            </div>
        </div>`;
}

// Render a single listing card
function renderCard(listing) {
    const groupMembers = (listing.group_member_list || []);
    const isGroup = groupMembers.length > 1;
    // Sold cards get a folded corner and a green outline: the item is finished,
    // so the card should say so before it is read.
    const isSold = Boolean(listing.is_sold) || listing.status === "sold";
    const platformList = isGroup
        ? [...new Set(groupMembers.map(m => m.platform))]
        : listing.platforms;
    const platformBadges = renderPlatformBadges(platformList, listing.platform);

    const price = listing.price_raw || `$${((listing.price_cents || 0) / 100).toFixed(2)} CAD`;
    const image = listing.image_url || "/static/img/placeholder.svg";

    // Prefer the server's verdict so the badge and the filter can never
    // disagree; fall back to the purchase price and the free flag, which is what
    // it is based on.
    const isFree = !!listing.cost_is_free;
    const missingCost = listing.missing_cost !== undefined
        ? !!listing.missing_cost
        : (!((listing.purchase_price_cents || 0) > 0) && !isFree);
    const hasCost = (listing.total_cost_cents || 0) > 0;
    const totalCostText = `$${(listing.total_cost || 0).toFixed(2)} CAD`;

    // Until an item sells there is no payout to reason from, so the best available
    // figure is the listed price less what it cost — and less the fee the platform
    // will take, estimated from the rates this account has actually been charged.
    // Without that last part an active card reads as pure profit.
    const estFees = listing.est_fees_cents || 0;
    const estNet = (((listing.price_cents || 0) - (listing.total_cost_cents || 0) - estFees) / 100);
    const netProfit = listing.net_profit !== undefined && !estFees
        ? listing.net_profit
        : estNet;
    const profitSign = netProfit > 0 ? "+" : (netProfit < 0 ? "−" : "");
    const profitColor = netProfit >= 0 ? "var(--success)" : "var(--danger)";

    // A completed sale has real economics: the marketplace's payout is what
    // actually arrived, after its fee and the buyer's postage. That is the only
    // figure worth showing as profit once it exists, because the listed price
    // overstates it by the fee every time.
    const payout = (listing.net_payout_cents || 0);
    const hasPayout = payout > 0;
    const actualProfit = listing.actual_profit !== undefined
        ? listing.actual_profit
        : ((payout || listing.price_cents || 0)
           - (listing.total_cost_cents || 0)
           - (listing.shipping_cost_cents || 0)) / 100;

    // With no purchase price there is no profit figure to trust: the listing
    // would otherwise show its full sale price as margin. Say so instead of
    // printing a misleading green number.
    //
    // Once the cost is known — including a deliberate $0 from "got it free" —
    // the profit is real and always shown, so a free item no longer displays no
    // cost information at all.
    // Everything spent on a sale: cost of goods plus the postage the seller paid.
    // Shown as its own figure so the profit is visibly the payout less this, rather
    // than a number that appears from nowhere.
    const costsOnSale = (listing.total_cost_cents || 0) + (listing.shipping_cost_cents || 0);

    // Free shipping is worth flagging: it is money the seller has decided to
    // absorb, and it changes what a sale is worth before anything else is
    // deducted. Only a definite true earns the badge — a platform that never
    // stated the terms must not be shown as either answer.
    const freeShippingChip = listing.free_shipping === true
        ? `<span class="card-chip card-chip--freeship" title="This listing advertises free shipping: you absorb the postage">Free shipping</span>`
        : "";

    const costLine = missingCost
        ? `<div class="card-cost-missing" title="No purchase price recorded for this listing">No COGS recorded — profit unknown</div>`
        : `
                    <div class="card-cost-line">
                        ${listing.status === 'written_off'
                            ? `<span>Written off · <span style="color: var(--danger); font-weight: 500;">Loss: −$${(listing.total_cost || 0).toFixed(2)} CAD</span></span>`
                            : (hasPayout
                                ? `<div class="card-cost-working"><span title="What the marketplace paid out, after its fee">Payout $${(payout / 100).toFixed(2)}</span>
                                   · <span title="Cost of goods plus the postage you paid">Costs $${(costsOnSale / 100).toFixed(2)}</span></div>
                                   <div class="card-actual-profit" style="color: ${actualProfit >= 0 ? 'var(--success)' : 'var(--danger)'};" title="Payout less all costs">${actualProfit >= 0 ? 'Profit +' : 'Loss −'}$${Math.abs(actualProfit).toFixed(2)} CAD</div>`
                                : `<div class="card-cost-working"><span>${isFree && !hasCost ? "Free" : `Cost: ${totalCostText}`}</span>${
                                       estFees
                                           ? ` · <span class="card-est-fee" title="${
                                                 listing.est_fee_basis === "category"
                                                     ? "Estimated from the fees this category has actually cost you"
                                                     : "Estimated from the fees this platform has actually cost you"
                                             } — ${((listing.est_fee_rate || 0) * 100).toFixed(1)}% of the listed price">Est. fees −$${(estFees / 100).toFixed(2)}</span>`
                                           : ""
                                   }</div>
                                   <div class="card-actual-profit" style="color: ${profitColor};" title="Listed price less costs and the estimated platform fee">Est. net: ${profitSign}$${Math.abs(netProfit).toFixed(2)} CAD</div>`)}
                    </div>
                `;

    const groupSummary = listing.group_summary;
    const delistAlert = (groupSummary && groupSummary.needs_delist)
        ? `<div class="card-delist-alert" title="This item sold on one channel but is still listed elsewhere.">
            Sold on ${escapeHtml((groupSummary.sold_platforms || []).join(", "))} — delist from ${escapeHtml((groupSummary.active_platforms || []).join(", "))}
        </div>`
        : "";

    const groupChip = isGroup
        ? `<span class="card-status card-status--group" title="Same item on ${groupMembers.length} channels">Group · ${groupMembers.length}</span>`
        : "";

    const metricsHtml = renderEngagementChip(listing);

    return `
        <div class="listing-card${isSold ? " listing-card--sold" : ""}${missingCost ? " listing-card--missing-cost" : ""}${isGroup ? " listing-card--group" : ""}" data-id="${listing.id}" data-platform="${listing.platform}">
            <span class="card-select-check">✓</span>
            <div class="card-platform-badge" style="display:flex;gap:4px;flex-wrap:wrap;max-width:85%;">${platformBadges}</div>
            <div class="card-image">
                <img src="${image}" alt="${escapeHtml(listing.title || 'Listing')}" loading="lazy" draggable="false" onerror="this.src='/static/img/placeholder.svg'">
            </div>
            <div class="card-body">
                <h3 class="card-title">${escapeHtml(listing.title || "Untitled")}</h3>
                <p class="card-price">${price}</p>
                ${delistAlert}
                ${costLine}
                <div class="card-flags">
                    <span class="card-status card-status--${listing.status || 'unknown'}">${listing.status === 'written_off' ? 'Written Off' : (listing.status || "unknown")}</span>
                    ${freeShippingChip}
                    ${groupChip}
                    ${missingCost ? `<span class="card-status card-status--missing-cost" title="No purchase price recorded">No Cost</span>` : ""}
                </div>
                ${metricsHtml ? `<div class="card-metrics">${metricsHtml}</div>` : ""}
                ${listing.team_name ? `<div class="card-footer"><span class="card-team" title="Team">${escapeHtml(listing.team_name)}</span></div>` : ""}
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

// ---------------------------------------------------------------------------
// Card density and back-to-top.
//
// Both are pure presentation, independent of the listing data, so they sit
// outside the data-loading flow: changing density must not refetch anything, and
// the back-to-top button has to work whatever the page length.
// ---------------------------------------------------------------------------
(function initViewControls() {
    const STORAGE_KEY = "marketplace.cardView";
    const VIEWS = ["comfortable", "compact"];
    const toggleButtons = Array.prototype.slice.call(
        document.querySelectorAll(".view-toggle-btn")
    );

    function applyView(view) {
        const chosen = VIEWS.indexOf(view) >= 0 ? view : "comfortable";
        document.body.dataset.cardView = chosen;
        toggleButtons.forEach(function (btn) {
            // aria-pressed rather than a class alone: it is the state the
            // assistive tech reads, and the styling hangs off the same attribute
            // so the two can never disagree.
            btn.setAttribute("aria-pressed", String(btn.dataset.view === chosen));
        });
        return chosen;
    }

    function remember(view) {
        try {
            window.localStorage.setItem(STORAGE_KEY, view);
        } catch (e) {
            // Private browsing or storage disabled. The toggle still works for
            // this view; it just will not be remembered.
        }
    }

    function recall() {
        try {
            return window.localStorage.getItem(STORAGE_KEY);
        } catch (e) {
            return null;
        }
    }

    applyView(recall());

    toggleButtons.forEach(function (btn) {
        btn.addEventListener("click", function () {
            remember(applyView(btn.dataset.view));
        });
    });

    // -- Back to top --------------------------------------------------------
    const backToTop = document.getElementById("back-to-top");
    if (!backToTop) {
        return;
    }

    // About a screen and a half. Below that the top is a short flick away and
    // the button would only be clutter over the inventory.
    const SHOW_AFTER = 600;
    let queued = false;

    function syncVisibility() {
        queued = false;
        backToTop.hidden = window.scrollY <= SHOW_AFTER;
    }

    window.addEventListener("scroll", function () {
        // One update per frame. The check is cheap but scroll fires constantly.
        if (queued) {
            return;
        }
        queued = true;
        window.requestAnimationFrame(syncVisibility);
    }, { passive: true });

    backToTop.addEventListener("click", function () {
        const reduced = window.matchMedia
            && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });

        // Scrolling alone does nothing for a keyboard or screen-reader user, so
        // move focus to the top of the page as well.
        const heading = document.querySelector("h1");
        if (heading) {
            heading.setAttribute("tabindex", "-1");
            heading.focus({ preventScroll: true });
        }
    });

    syncVisibility();
})();
