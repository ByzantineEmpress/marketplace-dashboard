/* List a Book on eBay.
   ================================================================

   Photograph the book, check what the catalogue returned, create a draft. Every
   field it fills is left editable: a catalogue record for one ISBN during
   development named a completely different book, so nothing here writes to eBay
   without passing through the seller's eyes. */

(function () {
    "use strict";

    var meta = {};
    var links = {};
    var images = [];

    function $(id) { return document.getElementById(id); }
    function value(id) {
        var el = $(id);
        return el ? el.value.trim() : "";
    }
    function setValue(id, v) {
        var el = $(id);
        if (el) el.value = v == null ? "" : v;
    }

    // -- reading an ISBN ---------------------------------------------------

    function status(text) {
        var el = $("bp-scan-status");
        if (el) el.textContent = text || "";
    }

    function scanFile(file) {
        if (!file) return;
        var preview = $("bp-preview");
        if (preview) {
            preview.src = URL.createObjectURL(file);
            preview.hidden = false;
        }
        status("Reading the photo…");

        var body = new FormData();
        body.append("file", file, file.name);

        fetch("/api/books/scan", { method: "POST", body: body })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.isbn) {
                    // Say which method found it. A barcode is exact; OCR is a
                    // guess that happened to pass the check digit, so the seller
                    // should know which one they are trusting.
                    status(data.method === "barcode"
                        ? "Found " + data.isbn + " from the barcode."
                        : "Read " + data.isbn + " from the printed ISBN — check it.");
                    setValue("bp-isbn", data.isbn);
                    lookup(data.isbn);
                } else {
                    status(data.error || "No ISBN found in that photo.");
                }
            })
            .catch(function (err) { status("Could not read the photo: " + err.message); });
    }

    // -- looking the book up ----------------------------------------------

    function lookup(isbn) {
        var query = isbn || value("bp-isbn-final") || value("bp-isbn");
        if (!query) { status("Enter or photograph an ISBN first."); return; }

        status("Looking up " + query + "…");
        fetch("/api/books/lookup?isbn=" + encodeURIComponent(query))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) { status(data.error || "Lookup failed."); return; }
                meta = data.meta || {};
                links = data.links || {};
                fill(data);
                status("");
            })
            .catch(function (err) { status("Lookup failed: " + err.message); });
    }

    function fill(data) {
        var m = data.meta || {};
        setValue("bp-title", m.title);
        setValue("bp-author", m.author);
        setValue("bp-publisher", m.publisher);
        setValue("bp-year", m.publication_year);
        setValue("bp-genre", m.genre);
        setValue("bp-topic", m.topic);
        setValue("bp-isbn-final", m.isbn13 || m.isbn10 || "");
        setValue("bp-format", m.format || "");
        setValue("bp-description", data.description || "");
        titleCount();

        var market = data.market || {};
        $("bp-available").textContent = market.available != null ? market.available : "—";
        $("bp-average").textContent = market.average_asking != null
            ? "$" + market.average_asking.toFixed(2) : "—";
        $("bp-range").textContent = (market.lowest_asking != null)
            ? "$" + market.lowest_asking.toFixed(2) + " – $" + market.highest_asking.toFixed(2)
            : "—";

        // Pre-fill the price from what others ask. A starting point, not a
        // recommendation, and it is left in the seller's hands.
        if (!$("bp-price").value && market.average_asking) {
            $("bp-price").value = market.average_asking.toFixed(2);
        }

        var note = $("bp-market-note");
        if (market.error) {
            note.textContent = "Could not read the market: " + market.error;
        } else if (!market.available) {
            note.textContent = "No copies of this edition are listed on eBay right now.";
        } else {
            note.textContent = "Asking prices, not sold prices — based on "
                + market.sampled + " of " + market.available + " listings.";
        }

        $("bp-link-active").href = links.active || "#";
        $("bp-link-sold").href = links.sold || "#";

        $("bp-listing").hidden = false;
        $("bp-market").hidden = false;
        $("bp-publish").hidden = false;
    }

    function titleCount() {
        var el = $("bp-title-count");
        if (el) el.textContent = String(value("bp-title").length);
    }

    // -- photos for the listing -------------------------------------------

    function renderThumbs() {
        var box = $("bp-thumbs");
        if (!box) return;
        box.innerHTML = images.map(function (url, i) {
            return '<span class="bp-thumb"><img src="' + url + '" alt="">' +
                '<button type="button" data-index="' + i + '" title="Remove">×</button></span>';
        }).join("");
        box.querySelectorAll("button").forEach(function (btn) {
            btn.addEventListener("click", function () {
                images.splice(Number(btn.dataset.index), 1);
                renderThumbs();
            });
        });
    }

    function uploadPhotos(files) {
        if (!files || !files.length) return;
        var state = $("bp-photo-status");
        var done = 0;
        state.textContent = "Uploading 0 of " + files.length + "…";

        var chain = Promise.resolve();
        Array.prototype.forEach.call(files, function (file) {
            chain = chain.then(function () {
                var body = new FormData();
                body.append("file", file, file.name);
                return fetch("/api/upload", { method: "POST", body: body })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        done += 1;
                        state.textContent = "Uploading " + done + " of " + files.length + "…";
                        if (data && (data.url || data.path)) {
                            images.push(data.url || data.path);
                        } else if (data && data.error) {
                            state.textContent = data.error;
                        }
                    })
                    .catch(function (err) { state.textContent = err.message; });
            });
        });
        chain.then(function () {
            state.textContent = images.length + " photo" + (images.length === 1 ? "" : "s") + " ready.";
            renderThumbs();
        });
    }

    // -- creating the draft ------------------------------------------------

    function draftPayload() {
        // Rebuild the description from the edited fields only if the seller has
        // not touched it; otherwise their text wins. Simpler and more predictable:
        // whatever is in the box is what is sent.
        return {
            title: value("bp-title"),
            description: value("bp-description"),
            price: parseFloat(value("bp-price")) || 0,
            currency: "CAD",
            quantity: 1,
            condition: value("bp-condition"),
            images: images,
            aspects: {
                "Author": value("bp-author") ? [value("bp-author")] : [],
                "Book Title": value("bp-title") ? [value("bp-title")] : [],
                "Publisher": value("bp-publisher") ? [value("bp-publisher")] : [],
                "Publication Year": value("bp-year") ? [value("bp-year")] : [],
                "ISBN": value("bp-isbn-final") ? [value("bp-isbn-final")] : [],
            },
        };
    }

    function createDraft() {
        var btn = $("bp-create-btn");
        var result = $("bp-result");
        btn.disabled = true;
        btn.textContent = "Creating draft…";
        result.hidden = true;

        fetch("/api/books/draft", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ draft: draftPayload(), confirm: true }),
        })
            .then(function (r) { return r.json().then(function (d) { return { res: r, data: d }; }); })
            .then(function (out) {
                result.hidden = false;
                if (!out.res.ok || !out.data.ok) {
                    result.className = "bp-result bp-result--fail";
                    result.textContent = out.data.error || ("HTTP " + out.res.status);
                    return;
                }
                result.className = "bp-result bp-result--ok";
                result.innerHTML =
                    "Draft created — offer <strong>" + out.data.offer_id + "</strong> " +
                    "(status " + out.data.status + ").<br>" +
                    '<a href="' + out.data.draft_url + '" target="_blank" rel="noopener">' +
                    "Open your eBay drafts →</a>";
                // The whole point of a draft is to go and finish it, so it opens
                // rather than waiting to be found.
                window.open(out.data.draft_url, "_blank", "noopener");
            })
            .catch(function (err) {
                result.hidden = false;
                result.className = "bp-result bp-result--fail";
                result.textContent = err.message;
            })
            .then(function () {
                btn.disabled = false;
                btn.textContent = "Create eBay draft";
            });
    }

    // -- wiring ------------------------------------------------------------

    document.addEventListener("DOMContentLoaded", function () {
        var file = $("bp-file");
        if (file) file.addEventListener("change", function () { scanFile(file.files[0]); });

        var photos = $("bp-photos");
        if (photos) photos.addEventListener("change", function () { uploadPhotos(photos.files); });

        if ($("bp-lookup-btn")) {
            $("bp-lookup-btn").addEventListener("click", function () { lookup(); });
        }
        var isbn = $("bp-isbn");
        if (isbn) {
            isbn.addEventListener("keydown", function (e) {
                if (e.key === "Enter") { e.preventDefault(); lookup(); }
            });
        }
        if ($("bp-title")) $("bp-title").addEventListener("input", titleCount);
        if ($("bp-create-btn")) $("bp-create-btn").addEventListener("click", createDraft);
    });
})();
