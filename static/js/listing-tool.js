/* Multi-platform listing tool (beta).
   ================================================================

   The page collects one item, asks each marketplace what it still needs, and
   publishes to the selected ones in a single action.

   It never publishes without an explicit confirmation: this creates live
   listings on real accounts, so the check is in the UI and again on the server. */

(function () {
    "use strict";

    var preflight = null;
    var selected = {};

    function $(id) { return document.getElementById(id); }

    function value(id) {
        var el = $(id);
        return el ? el.value.trim() : "";
    }

    function number(id, fallback) {
        var n = parseFloat(value(id));
        return isNaN(n) ? fallback : n;
    }

    // -- shared draft ------------------------------------------------------

    function lines(id) {
        return value(id).split(/\r?\n/).map(function (s) { return s.trim(); })
            .filter(Boolean);
    }

    function commas(id) {
        return value(id).split(",").map(function (s) { return s.trim(); })
            .filter(Boolean);
    }

    function draft() {
        return {
            title: value("f-title"),
            description: value("f-description"),
            price: number("f-price", 0),
            currency: value("f-currency") || "CAD",
            quantity: Math.max(1, parseInt(value("f-quantity"), 10) || 1),
            condition: value("f-condition"),
            sku: value("f-sku"),
            images: lines("f-images"),
            tags: commas("f-tags"),
            brand: value("f-brand"),
            aspects: value("f-brand") ? { Brand: [value("f-brand")] } : {},
            ebay_category_id: value("f-ebay-category"),
            policies: {
                fulfillment_policy_id: value("f-ebay-fulfillment"),
                payment_policy_id: value("f-ebay-payment"),
                return_policy_id: value("f-ebay-return")
            },
            taxonomy_id: value("f-etsy-taxonomy"),
            shipping_profile_id: value("f-etsy-shipping"),
            who_made: value("f-etsy-who"),
            when_made: value("f-etsy-when")
        };
    }

    // -- readiness ---------------------------------------------------------

    function renderPreflight() {
        var body = $("preflight-body");
        if (!preflight) {
            body.innerHTML = '<p class="lister-muted">No report yet.</p>';
            return;
        }
        var html = "";
        Object.keys(preflight.platforms).forEach(function (name) {
            var p = preflight.platforms[name];
            var state = p.ready ? "ready" : (p.connected ? "blocked" : "off");
            var label = p.ready ? "Ready" : (p.connected ? "Needs attention" : "Not connected");
            html += '<div class="preflight-row preflight-row--' + state + '">';
            html += '<div class="preflight-name">' + name + "</div>";
            html += '<div class="preflight-state">' + label + "</div>";
            if (p.problems && p.problems.length) {
                html += '<ul class="preflight-problems">';
                p.problems.forEach(function (problem) {
                    html += "<li>" + escapeHtml(problem) + "</li>";
                });
                html += "</ul>";
            }
            html += "</div>";
        });
        body.innerHTML = html;

        fillPlatforms();
        selected = {};
        Object.keys(preflight.platforms).forEach(function (name) {
            // Pre-tick only what can actually accept a listing, so the default
            // action cannot be a guaranteed failure.
            selected[name] = !!preflight.platforms[name].ready;
        });
        renderChoice();
        updateSummary();
    }

    function renderChoice() {
        var box = $("platform-choice");
        var html = "";
        Object.keys(preflight.platforms).forEach(function (name) {
            var p = preflight.platforms[name];
            var disabled = !p.connected;
            html += '<label class="platform-pick' + (disabled ? " platform-pick--off" : "") + '">' +
                '<input type="checkbox" data-platform="' + name + '"' +
                (selected[name] ? " checked" : "") + (disabled ? " disabled" : "") + ">" +
                "<span>" + name + "</span>" +
                '<span class="platform-pick-note">' +
                (p.ready ? "ready" : (p.connected ? "needs attention" : "not connected")) +
                "</span></label>";
        });
        box.innerHTML = html;

        box.querySelectorAll("input[data-platform]").forEach(function (box_) {
            box_.addEventListener("change", function () {
                selected[box_.dataset.platform] = box_.checked;
                toggleDetails();
                updateSummary();
            });
        });
        toggleDetails();
    }

    function toggleDetails() {
        if ($("ebay-detail")) $("ebay-detail").hidden = !selected.ebay;
        if ($("etsy-detail")) $("etsy-detail").hidden = !selected.etsy;
    }

    function fillSelect(id, options, labelFor) {
        var el = $(id);
        if (!el) return;
        var html = '<option value="">— choose —</option>';
        (options || []).forEach(function (option) {
            html += '<option value="' + escapeHtml(String(option.id)) + '">' +
                escapeHtml(labelFor(option)) + "</option>";
        });
        el.innerHTML = html;
    }

    function fillPlatforms() {
        var ebay = preflight.platforms.ebay || { options: {} };
        var etsy = preflight.platforms.etsy || { options: {} };
        var o = ebay.options || {};

        fillSelect("f-ebay-fulfillment", o.fulfillment_policies, function (p) {
            return p.name || p.id;
        });
        fillSelect("f-ebay-payment", o.payment_policies, function (p) {
            return p.name || p.id;
        });
        fillSelect("f-ebay-return", o.return_policies, function (p) {
            return p.name || p.id;
        });

        var e = etsy.options || {};
        fillSelect("f-etsy-shipping", e.shipping_profiles, function (p) {
            return (p.free ? "Free shipping — profile " : "Profile ") + p.id;
        });
        fillSelect("f-etsy-taxonomy", e.taxonomy, function (t) {
            return t.name || ("Taxonomy " + t.id);
        });
    }

    // -- summary and publish ----------------------------------------------

    function updateSummary() {
        var d = draft();
        var chosen = Object.keys(selected).filter(function (k) { return selected[k]; });
        var bits = [];
        if (d.title) bits.push(escapeHtml(d.title));
        bits.push("$" + (d.price || 0).toFixed(2) + " " + escapeHtml(d.currency));
        bits.push("qty " + d.quantity);
        if (chosen.length) {
            bits.push("to " + chosen.join(" + "));
        } else {
            bits.push("no marketplace selected");
        }
        $("publish-summary").innerHTML = bits.join(" · ");

        var ready = chosen.some(function (name) {
            return preflight && preflight.platforms[name] && preflight.platforms[name].ready;
        });
        $("publish-btn").disabled = !(ready && d.title && d.price > 0);
    }

    function loadPreflight() {
        $("preflight-body").innerHTML = '<p class="lister-muted">Checking what each marketplace needs…</p>';
        return fetch("/api/beta/listing/preflight")
            .then(function (res) { return res.json(); })
            .then(function (data) {
                preflight = data;
                renderPreflight();
            })
            .catch(function (err) {
                $("preflight-body").innerHTML =
                    '<p class="lister-muted">Could not check readiness: ' +
                    escapeHtml(err.message) + "</p>";
            });
    }

    function renderResult(data) {
        var card = $("result-card");
        var body = $("result-body");
        card.hidden = false;

        var html = "";
        Object.keys(data.results || {}).forEach(function (name) {
            var r = data.results[name];
            html += '<div class="result-row result-row--' + (r.ok ? "ok" : "fail") + '">';
            html += '<div class="result-name">' + name + "</div>";
            if (r.ok) {
                html += '<div class="result-detail">Listed as ' +
                    escapeHtml(String(r.listing_id || "?")) +
                    (r.url ? ' — <a href="' + escapeHtml(r.url) +
                        '" target="_blank" rel="noopener">open</a>' : "") + "</div>";
            } else {
                html += '<div class="result-detail">' + escapeHtml(r.error || "failed") + "</div>";
            }
            html += "</div>";
        });
        body.innerHTML = html;
        card.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function publish() {
        var button = $("publish-btn");
        var chosen = Object.keys(selected).filter(function (k) { return selected[k]; });
        if (!chosen.length) return;

        button.disabled = true;
        button.textContent = "Publishing…";

        fetch("/api/beta/listing/publish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                draft: draft(),
                platforms: chosen,
                // The one thing that keeps this from being an accidental publish.
                confirm: true
            })
        })
            .then(function (res) { return res.json().then(function (d) { return { res: res, data: d }; }); })
            .then(function (out) {
                if (!out.res.ok) {
                    renderResult({ results: { request: { ok: false, error: out.data.error || ("HTTP " + out.res.status) } } });
                } else {
                    renderResult(out.data);
                }
            })
            .catch(function (err) {
                renderResult({ results: { request: { ok: false, error: err.message } } });
            })
            .then(function () {
                button.textContent = "Publish to selected marketplaces";
                updateSummary();
            });
    }

    // -- wiring ------------------------------------------------------------

    document.addEventListener("DOMContentLoaded", function () {
        var title = $("f-title");
        if (title) {
            title.addEventListener("input", function () {
                $("title-count").textContent = String(title.value.length);
                updateSummary();
            });
        }

        ["f-price", "f-currency", "f-quantity", "f-description"]
            .forEach(function (id) {
                var el = $(id);
                if (el) el.addEventListener("input", updateSummary);
            });

        if ($("recheck-btn")) $("recheck-btn").addEventListener("click", loadPreflight);
        if ($("publish-btn")) $("publish-btn").addEventListener("click", publish);

        loadPreflight();
    });
})();
