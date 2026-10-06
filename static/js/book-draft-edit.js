/* Edit one book draft.
   ================================================================

   Saving touches two eBay objects, because a draft is made of two: the inventory
   item carries the title, description and photos, and the offer carries the price,
   quantity and category. Both PUTs replace rather than patch, so the server merges
   the edited fields into a full body it read first. The page just sends what
   changed.

   Nothing here publishes. Publishing is a separate act on the drafts page, behind
   its own confirmation, because one of these is reversible and the other is not. */

(function () {
    "use strict";

    var root = document.getElementById("be-root");
    if (!root) return;

    var offerId = root.dataset.offerId || "";
    var images = [];

    function $(id) { return document.getElementById(id); }
    function value(id) {
        var el = $(id);
        return el ? el.value.trim() : "";
    }
    function setValue(id, v) {
        var el = $(id);
        if (el) el.value = v === null || v === undefined ? "" : v;
    }

    function status(message, kind) {
        var el = $("be-status");
        if (!el) return;
        el.textContent = message || "";
        el.className = "bp-status" + (kind ? " settings-status--" + kind : "");
    }

    function titleCount() {
        var el = $("be-title-count");
        if (el) el.textContent = String(value("be-title").length);
    }

    function renderThumbs() {
        var box = $("be-thumbs");
        if (!box) return;
        box.innerHTML = "";
        if (!images.length) {
            box.appendChild(document.createTextNode("No photos yet."));
            return;
        }
        images.forEach(function (url, i) {
            var wrap = document.createElement("span");
            wrap.className = "bp-thumb";
            var link = document.createElement("a");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener";
            var img = document.createElement("img");
            img.src = url;
            img.alt = "";
            link.appendChild(img);
            wrap.appendChild(link);

            var remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "\u00d7";
            remove.title = "Remove this photo";
            remove.addEventListener("click", function () {
                images.splice(i, 1);
                renderThumbs();
            });
            wrap.appendChild(remove);
            box.appendChild(wrap);
        });
    }

    function load() {
        status("Loading the draft\u2026");
        fetch("/api/books/drafts/" + encodeURIComponent(offerId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) {
                    status(data.error || "Could not load that draft.", "error");
                    return;
                }
                var d = data.draft || {};
                setValue("be-title", d.title);
                setValue("be-price", d.price);
                setValue("be-quantity", d.quantity);
                setValue("be-category", d.category_id);
                setValue("be-description", d.description);
                setValue("be-weight", d.weight_kg);
                setValue("be-length", d.length_cm);
                setValue("be-width", d.width_cm);
                setValue("be-height", d.height_cm);
                images = (d.images || []).slice();
                renderThumbs();
                titleCount();

                $("be-meta").textContent =
                    "Offer " + (d.offer_id || "") + " · sku " + (d.sku || "")
                    + " · " + (d.status || "") + " · ships from "
                    + (d.location || "no location");
                $("be-card").hidden = false;
                status("");
            })
            .catch(function (err) {
                status("Could not load that draft: " + err.message, "error");
            });
    }

    function save() {
        var btn = $("be-save");
        btn.disabled = true;
        btn.textContent = "Saving\u2026";
        status("Saving\u2026");

        var changes = {
            title: value("be-title"),
            price: value("be-price"),
            quantity: parseInt(value("be-quantity"), 10) || 1,
            description: $("be-description").value,
            images: images,
            weight_kg: value("be-weight"),
            length_cm: value("be-length"),
            width_cm: value("be-width"),
            height_cm: value("be-height"),
        };
        var category = value("be-category");
        if (category) changes.category_id = category;

        fetch("/api/books/drafts/" + encodeURIComponent(offerId), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ changes: changes }),
        })
            .then(function (r) { return r.json().then(function (d) { return [r, d]; }); })
            .then(function (pair) {
                var res = pair[0], data = pair[1];
                if (!res.ok || !data.ok) {
                    status("Could not save: " + (data.error || res.status), "error");
                    return;
                }
                // Re-read what eBay stored rather than assuming the form's values
                // took: it is the only way to see that a merge went wrong.
                load();
                status("Saved.", "ok");
            })
            .catch(function (err) { status("Could not save: " + err.message, "error"); })
            .then(function () {
                btn.disabled = false;
                btn.textContent = "Save changes";
            });
    }

    function addPhotos(files) {
        if (!files || !files.length) return;
        var state = $("be-status");
        var done = 0;
        var chain = Promise.resolve();
        Array.prototype.forEach.call(files, function (file) {
            chain = chain.then(function () {
                var fd = new FormData();
                fd.append("file", file, file.name);
                return fetch("/api/upload", { method: "POST", body: fd })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        done += 1;
                        if (data && (data.url || data.path)) {
                            images.push(data.url || data.path);
                        } else if (data && data.error) {
                            status(data.error, "error");
                        }
                        if (state) state.textContent = "Uploading " + done + " of "
                            + files.length + "\u2026";
                    });
            });
        });
        chain.then(function () {
            renderThumbs();
            status(images.length + " photo" + (images.length === 1 ? "" : "s")
                   + " ready \u2014 press Save changes.", "ok");
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        if ($("be-title")) $("be-title").addEventListener("input", titleCount);
        if ($("be-save")) $("be-save").addEventListener("click", save);
        if ($("be-photos")) {
            $("be-photos").addEventListener("change", function () {
                addPhotos($("be-photos").files);
            });
        }
        if ($("be-delete")) {
            $("be-delete").addEventListener("click", function () {
                if (!window.confirm("Delete this draft? It is not on sale, so nothing "
                                    + "is lost from eBay.")) return;
                fetch("/api/books/drafts/" + encodeURIComponent(offerId),
                      { method: "DELETE" })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.ok) {
                            status("Could not delete: " + (data.error || ""), "error");
                            return;
                        }
                        window.location.href = "/books/drafts";
                    })
                    .catch(function (err) { status("Could not delete: " + err.message, "error"); });
            });
        }
        load();
    });
})();
