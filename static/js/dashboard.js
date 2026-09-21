/* ================================================================
   Dashboard page logic
   Loads listings/stats from the /api endpoints and wires up the
   search box, filters, and pagination controls.

   Note: lives in a static file (not inline) so the strict
   Content-Security-Policy (script-src 'self') still allows it.
   Shared helpers (escapeHtml) come from app.js, loaded first.
   ================================================================ */

document.addEventListener("DOMContentLoaded", () => {
    const state = { page: 1, search: "", platform: "", status: "", team: "", sort: "created_at", order: "desc" };
    const grid = document.getElementById("listing-grid");
    const pageInfo = document.getElementById("page-info");

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
        const res = await fetch(`/api/listings?${params}`);
        const data = await res.json();

        grid.innerHTML = data.listings.map(listing => renderCard(listing)).join("");
        pageInfo.textContent = `Page ${state.page}`;
        document.getElementById("stat-active").textContent = "—";  // populated by stats call
    }

    async function loadStats() {
        try {
            const teamParam = state.team ? `?team=${state.team}` : "";
            const res = await fetch(`/api/stats${teamParam}`);
            const stats = await res.json();
            document.getElementById("stat-active").textContent = stats.active_listings || 0;
            document.getElementById("stat-sold").textContent = stats.sold_listings || 0;
            document.getElementById("stat-total").textContent = stats.total_listings || 0;
            if (stats.total_value_cents) {
                const total = (stats.total_value_cents / 100).toFixed(2);
                document.getElementById("stat-value").textContent = `$${total}`;
            }
        } catch (e) { /* ignore — stats are nice-to-have */ }
    }

    loadStats();
    loadListings();

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

    // Teams: populate the "All My Teams" dropdown (only visible when the
    // user belongs to more than one team).
    async function loadTeams() {
        const sel = document.getElementById("filter-team");
        if (!sel) return;
        try {
            const res = await fetch("/api/teams");
            const teams = await res.json();
            if (Array.isArray(teams) && teams.length >= 2) {
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
        } catch (e) { /* single-team setups: nothing to filter by */ }
    }
    loadTeams();

    // Event: refresh button
    document.getElementById("refresh-btn").addEventListener("click", () => {
        loadListings();
        loadStats();
    });

    // Event: pagination
    document.getElementById("prev-page").addEventListener("click", () => {
        if (state.page > 1) { state.page--; loadListings(); }
    });
    document.getElementById("next-page").addEventListener("click", () => {
        state.page++; loadListings();
    });
});

// Render a single listing card (called by the load function above)
function renderCard(listing) {
    const platformIcon = getPlatformIcon(listing.platform);
    const price = listing.price_raw || `$${((listing.price_cents || 0) / 100).toFixed(2)}`;
    const image = listing.image_url || "/static/img/placeholder.svg";

    return `
        <div class="listing-card" data-platform="${listing.platform}">
            <div class="card-platform-badge">${platformIcon}</div>
            <div class="card-image">
                <img src="${image}" alt="${escapeHtml(listing.title || 'Listing')}" loading="lazy">
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
    return icons[platform] || `<span class="platform-icon">${platform}</span>`;
}
