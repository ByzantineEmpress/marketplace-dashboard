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

    // -- eBay category picker ---------------------------------------------

    var chosenCategory = null;
    var aspects = {};

    function suggestCategories() {
        var query = value("f-ebay-category-search") || value("f-title");
        var box = $("category-results");
        if (!query) {
            box.innerHTML = '<p class="lister-muted">Type a title first, or describe the item.</p>';
            return;
        }
        box.innerHTML = '<p class="lister-muted">Looking up categories…</p>';
        fetch("/api/beta/listing/categories?q=" + encodeURIComponent(query))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var list = data.suggestions || [];
                if (!list.length) {
                    box.innerHTML = '<p class="lister-muted">' +
                        escapeHtml(data.error || "No category suggestions for that.") + "</p>";
                    return;
                }
                box.innerHTML = list.map(function (c) {
                    return '<button type="button" class="category-option" data-id="' +
                        escapeHtml(String(c.id)) + '" data-name="' +
                        escapeHtml(c.name || "") + '">' +
                        escapeHtml(c.path || c.name || c.id) + "</button>";
                }).join("");
                box.querySelectorAll(".category-option").forEach(function (btn) {
                    btn.addEventListener("click", function () {
                        chooseCategory(btn.dataset.id, btn.dataset.name);
                    });
                });
            })
            .catch(function (err) {
                box.innerHTML = '<p class="lister-muted">Could not look up categories: ' +
                    escapeHtml(err.message) + "</p>";
            });
    }

    function chooseCategory(id, name) {
        chosenCategory = { id: id, name: name };
        $("f-ebay-category").value = id;
        $("category-chosen").textContent = "Chosen: " + name + " (" + id + ")";
        $("category-results").innerHTML = "";
        loadAspects(id);
    }

    function loadAspects(categoryId) {
        var field = $("aspects-field");
        var list = $("aspects-list");
        list.innerHTML = '<p class="lister-muted">Checking what eBay requires…</p>';
        field.hidden = false;
        fetch("/api/beta/listing/aspects?category_id=" + encodeURIComponent(categoryId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var required = data.required || [];
                var recommended = data.recommended || [];
                if (!required.length && !recommended.length) {
                    list.innerHTML = '<p class="lister-muted">' +
                        escapeHtml(data.error || "This category asks for nothing extra.") + "</p>";
                    return;
                }
                var html = "";
                required.forEach(function (a) { html += aspectField(a, true); });
                // Only a couple of the optional ones: eBay returns dozens and the
                // form stops being a form.
                recommended.slice(0, 4).forEach(function (a) { html += aspectField(a, false); });
                list.innerHTML = html;
            })
            .catch(function (err) {
                list.innerHTML = '<p class="lister-muted">Could not read the category: ' +
                    escapeHtml(err.message) + "</p>";
            });
    }

    function aspectField(aspect, required) {
        var id = "aspect-" + aspect.name.replace(/[^A-Za-z0-9]+/g, "-");
        var prefilled = prefillFor(aspect.name);
        return '<label class="aspect' + (required ? " aspect--required" : "") + '">' +
            '<span class="aspect-name">' + escapeHtml(aspect.name) +
            (required ? ' <span class="aspect-req">required</span>' : "") + "</span>" +
            '<input type="text" id="' + id + '" data-aspect="' + escapeHtml(aspect.name) +
            '" value="' + escapeHtml(prefilled) + '"' +
            (aspect.values && aspect.values.length
                ? ' list="' + id + '-list"><datalist id="' + id + '-list">' +
                  aspect.values.map(function (v) {
                      return '<option value="' + escapeHtml(v) + '"></option>';
                  }).join("") + "</datalist>"
                : ">") +
            "</label>";
    }

    // The shared fields already answer some of what eBay asks. Filling those in
    // is the whole point of entering the item once.
    function prefillFor(name) {
        var key = name.toLowerCase();
        if (key.indexOf("brand") !== -1) return value("f-brand");
        if (key === "type" || key.indexOf("product type") !== -1) return "";
        if (key.indexOf("model") !== -1) return value("f-brand");
        return "";
    }

    function collectAspects() {
        var out = {};
        document.querySelectorAll("#aspects-list input[data-aspect]").forEach(function (input) {
            var v = input.value.trim();
            if (v) out[input.dataset.aspect] = [v];
        });
        return out;
    }

    // -- tags --------------------------------------------------------------

    // Etsy takes up to 13 tags, and buyers find things by them. The title is
    // already the best description of the item, so words from it beat asking the
    // seller to retype the same idea in a different box.
    var TAG_STOPWORDS = {
        the: 1, and: 1, with: 1, for: 1, from: 1, this: 1, that: 1, you: 1,
        new: 1, used: 1, works: 1, working: 1, tested: 1, includes: 1, only: 1,
        rare: 1, oem: 1, original: 1, authentic: 1, vintage: 0
    };

    function suggestTags() {
        var title = value("f-title").toLowerCase();
        var existing = {};
        commas("f-tags").forEach(function (t) { existing[t.toLowerCase()] = 1; });

        var words = title.replace(/[^a-z0-9\s]/g, " ").split(/\s+/)
            .filter(function (w) { return w.length > 2 && !TAG_STOPWORDS[w]; });

        var tags = [];
        // Two-word phrases first: they are more specific than single words and
        // are what a search actually matches on.
        for (var i = 0; i < words.length - 1 && tags.length < 6; i++) {
            var pair = words[i] + " " + words[i + 1];
            if (!existing[pair] && tags.indexOf(pair) === -1) tags.push(pair);
        }
        words.forEach(function (w) {
            if (tags.length < 13 && !existing[w] && tags.indexOf(w) === -1) tags.push(w);
        });

        var merged = commas("f-tags");
        tags.forEach(function (t) {
            if (merged.length < 13 && merged.indexOf(t) === -1) merged.push(t);
        });
        $("f-tags").value = merged.slice(0, 13).join(", ");
        updateSummary();
    }

    // -- image upload ------------------------------------------------------

    function renderThumbs() {
        var urls = lines("f-images");
        var box = $("upload-thumbs");
        box.innerHTML = urls.map(function (url, index) {
            return '<span class="thumb"><img src="' + escapeHtml(url) + '" alt=""' +
                ' onerror="this.style.opacity=0.3">' +
                '<button type="button" class="thumb-remove" data-index="' + index +
                '" title="Remove">×</button></span>';
        }).join("");
        box.querySelectorAll(".thumb-remove").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var idx = Number(btn.dataset.index);
                var current = lines("f-images");
                current.splice(idx, 1);
                $("f-images").value = current.join("\n");
                renderThumbs();
                updateSummary();
            });
        });
    }

    function uploadFiles(files) {
        if (!files || !files.length) return;
        var status = $("upload-status");
        var total = files.length;
        var done = 0;
        status.textContent = "Uploading 0 of " + total + "…";

        var chain = Promise.resolve();
        Array.prototype.forEach.call(files, function (file) {
            chain = chain.then(function () {
                var body = new FormData();
                body.append("file", file, file.name);
                return fetch("/api/upload", { method: "POST", body: body })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        done += 1;
                        status.textContent = "Uploading " + done + " of " + total + "…";
                        if (data && (data.url || data.path)) {
                            var current = lines("f-images");
                            current.push(data.url || data.path);
                            $("f-images").value = current.join("\n");
                        } else {
                            status.textContent = "Upload failed: " +
                                ((data && data.error) || "unknown error");
                        }
                    })
                    .catch(function (err) {
                        done += 1;
                        status.textContent = "Upload failed: " + err.message;
                    });
            });
        });
        chain.then(function () {
            status.textContent = done + " photo" + (done === 1 ? "" : "s") + " ready.";
            renderThumbs();
            updateSummary();
        });
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
            // Whatever the category asked for, plus Brand from the shared field,
            // so eBay gets the specifics it requires without them being typed
            // twice.
            aspects: (function () {
                var out = collectAspects();
                if (value("f-brand") && !out.Brand) out.Brand = [value("f-brand")];
                return out;
            })(),
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

    function preselectOnly(id, options) {
        var el = $(id);
        if (!el) return;
        if (!el.value && options && options.length === 1) {
            el.value = String(options[0].id);
        }
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

        // A single policy on the account is almost always the intended one, so
        // choosing it saves a click that carries no decision.
        preselectOnly("f-ebay-fulfillment", o.fulfillment_policies);
        preselectOnly("f-ebay-payment", o.payment_policies);
        preselectOnly("f-ebay-return", o.return_policies);

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
        if ($("category-search-btn")) {
            $("category-search-btn").addEventListener("click", suggestCategories);
        }
        var categorySearch = $("f-ebay-category-search");
        if (categorySearch) {
            // Enter is what anyone types after describing the item; making them
            // reach for the button instead would be a small daily annoyance.
            categorySearch.addEventListener("keydown", function (e) {
                if (e.key === "Enter") {
                    e.preventDefault();
                    suggestCategories();
                }
            });
        }
        if ($("suggest-tags-btn")) {
            $("suggest-tags-btn").addEventListener("click", suggestTags);
        }
        var files = $("f-files");
        if (files) {
            files.addEventListener("change", function () { uploadFiles(files.files); });
        }

        loadPreflight();
        renderThumbs();
    });
})();
