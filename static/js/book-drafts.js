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

        if (draft.image) {
            var img = el("img", "bd-thumb");
            img.src = draft.image;
            img.alt = "";
            img.loading = "lazy";
            card.appendChild(img);
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
        drafts.forEach(function (draft) { root.appendChild(row(draft)); });
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
