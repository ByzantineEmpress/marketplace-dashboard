/* Book drafts: the seller's unpublished eBay offers.
   ================================================================

   eBay's own drafts page does not list offers created through the Inventory API, so
   a draft is otherwise invisible until it is published. This page reads them from
   the API and gives them a home.

   Publish is the irreversible one: it creates a live listing buyers can purchase.
   Delete only removes an unpublished offer. They are treated differently on
   purpose, down to the wording of the confirmation. */

(function () {
    "use strict";

    var root = document.getElementById("bd-drafts");
    if (!root) return;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function setStatus(message, kind) {
        var status = document.getElementById("bd-status");
        if (!status) return;
        status.textContent = message || "";
        status.className = "settings-status" + (kind ? " settings-status--" + kind : "");
    }

    function money(value, currency) {
        var n = parseFloat(value);
        if (isNaN(n)) return "—";
        return "$" + n.toFixed(2) + " " + (currency || "");
    }

    function row(draft) {
        var card = el("div", "bd-row");
        card.dataset.offerId = draft.offer_id;

        var check = document.createElement("input");
        check.type = "checkbox";
        check.className = "bd-check";
        check.value = draft.offer_id;
        check.setAttribute("aria-label", "Select " + (draft.title || "this draft"));
        card.appendChild(check);

        if (draft.image) {
            // A link rather than a plain image: the thumbnail is too small to judge
            // a book's condition from, so clicking opens the full-size photo.
            var link = document.createElement("a");
            link.href = draft.image;
            link.target = "_blank";
            link.rel = "noopener";
            link.title = "Open the full-size photo";
            var img = el("img", "bd-thumb");
            img.src = draft.image;
            img.alt = draft.title || "";
            img.loading = "lazy";
            link.appendChild(img);
            card.appendChild(link);
        } else {
            card.appendChild(el("span", "bd-nophoto", "no photo"));
        }

        var main = el("div", "bd-main");
        main.appendChild(el("div", "bd-title", draft.title || "(untitled)"));

        var facts = [
            money(draft.price, draft.currency),
            "category " + (draft.category_id || "—"),
            draft.location ? "ships from " + draft.location : "no location",
            draft.status,
        ];
        main.appendChild(el("div", "bd-facts", facts.join("  ·  ")));
        card.appendChild(main);

        var actions = el("div", "bd-actions");

        var publish = el("button", "btn btn--sm btn--success", "Publish");
        publish.type = "button";
        publish.addEventListener("click", function () {
            // Said plainly, because this one cannot be undone from here.
            var ok = window.confirm(
                "Publish \"" + (draft.title || "this draft") + "\"?\n\n"
                + "This creates a LIVE eBay listing at " + money(draft.price, draft.currency)
                + " that buyers can purchase immediately. It is not a draft any more."
            );
            if (!ok) return;
            publish.disabled = true;
            publish.textContent = "Publishing…";
            fetch("/api/books/drafts/" + encodeURIComponent(draft.offer_id) + "/publish", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirm: true }),
            })
                .then(function (r) { return r.json().then(function (d) { return [r, d]; }); })
                .then(function (pair) {
                    var res = pair[0], data = pair[1];
                    if (!res.ok || !data.ok) {
                        setStatus("Could not publish: " + (data.error || res.status), "error");
                        publish.disabled = false;
                        publish.textContent = "Publish";
                        return;
                    }
                    setStatus("Published as listing " + data.listing_id + ".", "ok");
                    load();
                })
                .catch(function (err) {
                    setStatus("Could not publish: " + err.message, "error");
                    publish.disabled = false;
                    publish.textContent = "Publish";
                });
        });

        var del = el("button", "btn btn--sm btn--danger", "Delete");
        del.type = "button";
        del.addEventListener("click", function () {
            var ok = window.confirm(
                "Delete the draft \"" + (draft.title || "") + "\"?\n\n"
                + "It is not on sale, so nothing is lost from eBay — the offer is "
                + "simply removed."
            );
            if (!ok) return;
            del.disabled = true;
            del.textContent = "Deleting…";
            fetch("/api/books/drafts/" + encodeURIComponent(draft.offer_id)
                  + "?sku=" + encodeURIComponent(draft.sku || ""), { method: "DELETE" })
                .then(function (r) { return r.json().then(function (d) { return [r, d]; }); })
                .then(function (pair) {
                    var res = pair[0], data = pair[1];
                    if (!res.ok || !data.ok) {
                        setStatus("Could not delete: " + (data.error || res.status), "error");
                        del.disabled = false;
                        del.textContent = "Delete";
                        return;
                    }
                    setStatus("Draft deleted.", "ok");
                    load();
                })
                .catch(function (err) {
                    setStatus("Could not delete: " + err.message, "error");
                    del.disabled = false;
                    del.textContent = "Delete";
                });
        });

        actions.appendChild(publish);
        actions.appendChild(del);
        card.appendChild(actions);
        return card;
    }

    function render(drafts) {
        root.textContent = "";
        if (!drafts.length) {
            root.appendChild(el("p", "section-desc",
                "No drafts. Anything you create on the List a Book page waits here "
                + "until you publish it."));
            return;
        }

        // A toolbar rather than a per-row delete only: clearing out several drafts
        // one confirmation at a time is the tedious part.
        var bar = el("div", "bd-bar");
        var all = document.createElement("input");
        all.type = "checkbox";
        all.id = "bd-select-all";
        var allLabel = el("label", "bd-bar-label", "Select all");
        allLabel.setAttribute("for", "bd-select-all");
        allLabel.insertBefore(all, allLabel.firstChild);

        var count = el("span", "bd-bar-count", "");
        var bulk = el("button", "btn btn--sm btn--danger", "Delete selected");
        bulk.type = "button";
        bulk.disabled = true;

        function selected() {
            return Array.prototype.slice
                .call(root.querySelectorAll(".bd-check"))
                .filter(function (c) { return c.checked; })
                .map(function (c) { return c.value; });
        }

        function sync() {
            var n = selected().length;
            count.textContent = n ? n + " selected" : "";
            bulk.disabled = n === 0;
            all.checked = n > 0 && n === root.querySelectorAll(".bd-check").length;
        }

        all.addEventListener("change", function () {
            Array.prototype.forEach.call(root.querySelectorAll(".bd-check"),
                function (c) { c.checked = all.checked; });
            sync();
        });
        root.addEventListener("change", function (event) {
            if (event.target && event.target.classList.contains("bd-check")) sync();
        });

        bulk.addEventListener("click", function () {
            var ids = selected();
            if (!ids.length) return;
            var ok = window.confirm(
                "Delete " + ids.length + " draft" + (ids.length === 1 ? "" : "s") + "?\n\n"
                + "None of them are on sale, so nothing is lost from eBay -- the "
                + "offers are simply removed."
            );
            if (!ok) return;

            bulk.disabled = true;
            var done = 0, failed = [];

            // Sequential rather than parallel: eBay rate-limits, and a partial
            // failure is easier to report honestly one at a time.
            var chain = Promise.resolve();
            ids.forEach(function (id) {
                chain = chain.then(function () {
                    var draft = drafts.filter(function (d) {
                        return d.offer_id === id;
                    })[0] || {};
                    bulk.textContent = "Deleting " + (done + 1) + " of " + ids.length + "…";
                    return fetch("/api/books/drafts/" + encodeURIComponent(id)
                                 + "?sku=" + encodeURIComponent(draft.sku || ""),
                                 { method: "DELETE" })
                        .then(function (r) { return r.json(); })
                        .then(function (data) {
                            if (data && data.ok) { done += 1; }
                            else { failed.push(id); }
                        })
                        .catch(function () { failed.push(id); });
                });
            });

            chain.then(function () {
                if (failed.length) {
                    setStatus("Deleted " + done + " of " + ids.length + "; "
                              + failed.length + " failed.", "error");
                } else {
                    setStatus("Deleted " + done + " draft"
                              + (done === 1 ? "" : "s") + ".", "ok");
                }
                load();
            });
        });

        bar.appendChild(allLabel);
        bar.appendChild(count);
        bar.appendChild(bulk);
        root.appendChild(bar);

        drafts.forEach(function (draft) { root.appendChild(row(draft)); });
        sync();
    }

    function load() {
        setStatus("Loading…");
        fetch("/api/books/drafts")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) {
                    // Say what went wrong rather than sitting on "Loading…", which
                    // is what hid a failed fetch on the settings page.
                    setStatus(data.error || "Could not load your drafts.", "error");
                    root.textContent = "";
                    return;
                }
                setStatus(data.count + (data.count === 1 ? " draft" : " drafts"));
                render(data.drafts || []);
            })
            .catch(function (err) {
                setStatus("Could not load your drafts: " + err.message, "error");
                root.textContent = "";
            });
    }

    load();
})();
