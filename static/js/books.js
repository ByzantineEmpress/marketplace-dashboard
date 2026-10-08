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

    // -- the publish confirmation -------------------------------------------
    //
    // A native confirm() cannot be styled, and this is the one action on the page
    // that puts something up for sale -- so it gets a dialog that shows the price
    // being agreed to at a size the eye lands on. Promise-wrapped so the calling
    // code still reads as "ask, then publish".
    function askToPublish(title, price) {
        var backdrop = $("bp-confirm");
        if (!backdrop) return Promise.resolve(false);   // never block a fallback

        // textContent, not setValue: that helper writes .value, which a <p> does not
        // use -- the title would simply never appear.
        var book = $("bp-confirm-book");
        if (book) book.textContent = title;
        var amount = $("bp-confirm-price");
        if (amount) {
            var value_ = parseFloat(price);
            amount.textContent = "$" + (isNaN(value_) ? price : value_.toFixed(2));
        }
        backdrop.classList.add("is-open");
        backdrop.setAttribute("aria-hidden", "false");
        var go = $("bp-confirm-go");
        if (go) go.focus();

        return new Promise(function (resolve) {
            function close(answer) {
                backdrop.classList.remove("is-open");
                backdrop.setAttribute("aria-hidden", "true");
                document.removeEventListener("keydown", onKey);
                if (go) go.removeEventListener("click", onGo);
                ["bp-confirm-cancel", "bp-confirm-x"].forEach(function (id) {
                    var el = $(id);
                    if (el) el.removeEventListener("click", onCancel);
                });
                backdrop.removeEventListener("click", onBackdrop);
                resolve(answer);
            }
            function onGo() { close(true); }
            function onCancel() { close(false); }
            // Tapping the dim area behind the dialog cancels, which is what people
            // expect and is safer than it confirming.
            function onBackdrop(event) {
                if (event.target === backdrop) close(false);
            }
            function onKey(event) {
                if (event.key === "Escape") close(false);
            }

            if (go) go.addEventListener("click", onGo);
            ["bp-confirm-cancel", "bp-confirm-x"].forEach(function (id) {
                var el = $(id);
                if (el) el.addEventListener("click", onCancel);
            });
            backdrop.addEventListener("click", onBackdrop);
            document.addEventListener("keydown", onKey);
        });
    }

    // -- taking several photos in one go -----------------------------------
    //
    // The camera app closes after every single photo, so a book needing a barcode, a
    // cover and a title page means opening it three times and being kicked back to
    // the form each time. This keeps the viewfinder up and accumulates shots until
    // the seller says they are done.

    var captureStream = null;
    var shots = [];

    function captureCount() {
        var el = $("pc-count");
        if (!el) return;
        el.textContent = shots.length
            ? shots.length + (shots.length === 1 ? " photo" : " photos")
            : "No photos yet";
        var done = $("pc-done");
        if (done) done.disabled = shots.length === 0;
    }

    function stopCapture() {
        if (captureStream) {
            captureStream.getTracks().forEach(function (t) { t.stop(); });
            captureStream = null;
        }
        var video = $("pc-video");
        if (video) video.srcObject = null;
        var overlay = $("pc-overlay");
        if (overlay) overlay.hidden = true;
    }

    function snap() {
        var shutter = $("pc-shutter");
        if (shutter) shutter.disabled = true;
        grabFrame("pc-video")
            .then(function (blob) {
                if (!blob) throw new Error("the camera was not ready");
                shots.push(blob);
                // Shown straight away so a blurred shot can be retaken before
                // leaving, rather than after uploading.
                var strip = $("pc-strip");
                if (strip) {
                    var thumb = document.createElement("img");
                    thumb.src = URL.createObjectURL(blob);
                    thumb.alt = "";
                    strip.appendChild(thumb);
                    strip.scrollLeft = strip.scrollWidth;
                }
                captureCount();
            })
            .catch(function (err) {
                var count = $("pc-count");
                if (count) count.textContent = "Could not take that photo: " + err.message;
            })
            .then(function () {
                // Deliberately NOT closing: the whole point is another shot.
                if (shutter) shutter.disabled = false;
            });
    }

    function finishCapture() {
        var taken = shots.slice();
        stopCapture();
        shots = [];
        var strip = $("pc-strip");
        if (strip) strip.textContent = "";
        if (!taken.length) return;
        var state = $("bp-photo-status");
        if (state) state.textContent = "Uploading " + taken.length + " photo"
            + (taken.length === 1 ? "" : "s") + "…";

        var done = 0;
        var chain = Promise.resolve();
        taken.forEach(function (blob) {
            chain = chain.then(function () {
                var body = new FormData();
                body.append("file", blob, "photo-" + (done + 1) + ".jpg");
                return fetch("/api/upload", { method: "POST", body: body })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        done += 1;
                        if (data && (data.url || data.path)) {
                            images.push(data.url || data.path);
                        } else if (data && data.error && state) {
                            state.textContent = data.error;
                        }
                        if (state) {
                            state.textContent = "Uploading " + done + " of "
                                + taken.length + "…";
                        }
                    })
                    .catch(function (err) {
                        if (state) state.textContent = "Upload failed: " + err.message;
                    });
            });
        });
        chain.then(function () {
            renderThumbs();
            if (state) {
                state.textContent = images.length + " photo"
                    + (images.length === 1 ? "" : "s") + " ready.";
            }
        });
    }

    function startCapture() {
        var overlay = $("pc-overlay");
        var video = $("pc-video");
        if (!overlay || !video) return;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            status("This browser cannot open the camera here — use the library option.");
            return;
        }

        shots = [];
        var strip = $("pc-strip");
        if (strip) strip.textContent = "";
        captureCount();
        overlay.hidden = false;

        navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: "environment" } },
            audio: false,
        })
            .then(function (s) {
                captureStream = s;
                video.srcObject = s;
                return video.play();
            })
            .catch(function (err) {
                stopCapture();
                status("Could not open the camera: " + err.message
                       + " — use the library option instead.");
            });
    }

    // -- surviving a reload ------------------------------------------------
    //
    // After a book is created or published the form is cleared, because the next
    // action is always another book and retyping over the last one's data is how
    // details get carried across by mistake. Clearing means reloading, and a reload
    // would take the confirmation with it -- so the result is stashed and collected
    // again on the way back.

    var LAST_RESULT = "bp-last-result";

    function stashResult(html, kind) {
        try {
            sessionStorage.setItem(LAST_RESULT,
                                   JSON.stringify({ html: html, kind: kind }));
        } catch (err) {
            // Private mode can refuse storage. Losing the banner is better than
            // failing a publish that has already happened.
        }
    }

    function showStashedResult() {
        var box = $("bp-result");
        if (!box) return;
        var raw = null;
        try {
            raw = sessionStorage.getItem(LAST_RESULT);
            sessionStorage.removeItem(LAST_RESULT);
        } catch (err) {
            return;
        }
        if (!raw) return;
        try {
            var saved = JSON.parse(raw);
            box.hidden = false;
            box.className = "bp-result bp-result--" + (saved.kind || "ok");
            box.innerHTML = saved.html;
        } catch (err) {
            // Unreadable stash: nothing to show, and nothing worth breaking over.
        }
    }

    // -- scanning ----------------------------------------------------------
    //
    // A live viewfinder rather than the camera app. Going out to the camera, taking
    // a shot, keeping it and coming back is the slowest possible way to read a
    // barcode that is already in front of the lens -- and it leaves a junk photo on
    // the phone each time.
    //
    // Where the browser can decode barcodes itself (BarcodeDetector, which is
    // Chrome on Android) it does so continuously and the ISBN appears with no tap at
    // all. Where it cannot (Safari on iOS) the viewfinder still shows framing and a
    // Capture button grabs a single frame and sends it down the same decode path --
    // no file, no camera roll, no file picker.

    var stream = null;
    var scanning = false;
    var detector = null;

    function scanStatus(text) {
        var el = $("bs-hint");
        if (el) el.textContent = text;
    }

    function stopScanner() {
        scanning = false;
        if (stream) {
            stream.getTracks().forEach(function (track) { track.stop(); });
            stream = null;
        }
        var video = $("bs-video");
        if (video) video.srcObject = null;
        var overlay = $("bs-overlay");
        if (overlay) overlay.hidden = true;
    }

    function sendFrame(blob) {
        // Reuses the same endpoint as the photo path, so the decode rules -- barcode
        // first, printed ISBN as the fallback -- are the same either way.
        var body = new FormData();
        body.append("file", blob, "scan.jpg");
        return fetch("/api/books/scan", { method: "POST", body: body })
            .then(function (r) { return r.json(); });
    }

    function acceptScan(data) {
        if (!data || !data.isbn) return false;
        stopScanner();
        status(data.method === "barcode"
            ? "Found " + data.isbn + " from the barcode."
            : "Read " + data.isbn + " from the printed ISBN — check it.");
        setValue("bp-isbn", data.isbn);
        lookup(data.isbn);
        return true;
    }

    function grabFrame(videoId) {
        var video = $(videoId || "bs-video");
        if (!video || !video.videoWidth) return Promise.resolve(null);
        var canvas = document.createElement("canvas");
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext("2d").drawImage(video, 0, 0);
        return new Promise(function (resolve) {
            canvas.toBlob(function (blob) { resolve(blob); }, "image/jpeg", 0.9);
        });
    }

    function liveDetect() {
        if (!scanning) return;
        var video = $("bs-video");
        if (detector && video && video.readyState === video.HAVE_ENOUGH_DATA) {
            detector.detect(video)
                .then(function (codes) {
                    if (!scanning) return;
                    if (codes && codes.length && codes[0].rawValue) {
                        // The digits are already an ISBN, so they go straight to the
                        // lookup rather than being posted as a fake image for the
                        // server to decode. The lookup validates the check digit and
                        // reports a bad read, so a misread cannot slip through.
                        stopScanner();
                        status("Found " + codes[0].rawValue + " from the barcode.");
                        setValue("bp-isbn", codes[0].rawValue);
                        lookup(codes[0].rawValue);
                        return;
                    }
                    setTimeout(liveDetect, 250);
                })
                .catch(function () { setTimeout(liveDetect, 400); });
        } else {
            setTimeout(liveDetect, 400);
        }
    }

    function startScanner() {
        var overlay = $("bs-overlay");
        var video = $("bs-video");
        if (!overlay || !video) return;

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            // No in-page camera: fall back to the file input, which still works.
            status("This browser cannot open the camera here — use the photo option.");
            return;
        }

        overlay.hidden = false;
        scanStatus("Starting the camera…");

        navigator.mediaDevices.getUserMedia({
            video: { facingMode: { ideal: "environment" } },
            audio: false,
        })
            .then(function (s) {
                stream = s;
                video.srcObject = s;
                return video.play();
            })
            .then(function () {
                scanning = true;
                // BarcodeDetector decodes live where it exists, which turns this into
                // a true scanner: no tap needed at all.
                if ("BarcodeDetector" in window) {
                    detector = new window.BarcodeDetector({
                        formats: ["ean_13", "ean_8"],
                    });
                    scanStatus("Point at the barcode — it reads by itself");
                    liveDetect();
                } else {
                    detector = null;
                    scanStatus("Line the barcode up inside the frame, then Capture");
                }
            })
            .catch(function (err) {
                stopScanner();
                status("Could not open the camera: " + err.message
                       + " — use the photo option instead.");
            });
    }

    function captureFrame() {
        var button = $("bs-shutter");
        if (button) { button.disabled = true; button.textContent = "Reading…"; }
        grabFrame()
            .then(function (blob) {
                if (!blob) throw new Error("the camera was not ready");
                return sendFrame(blob);
            })
            .then(function (data) {
                if (acceptScan(data)) return;
                scanStatus((data && data.error) || "No ISBN found — try again");
            })
            .catch(function (err) { scanStatus("Could not read it: " + err.message); })
            .then(function () {
                if (button) { button.disabled = false; button.textContent = "Capture"; }
            });
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

        keyNote(m);
        renderSearches(data.links || {});

        $("bp-listing").hidden = false;
        $("bp-market").hidden = false;
        $("bp-publish").hidden = false;
    }

    // Google Books is the second catalogue and the fields above are thinner
    // without it, so a refusal is worth saying out loud. Both of these are
    // actionable, and neither shows up anywhere else on the page.
    function keyNote(m) {
        var el = $("bp-key-note");
        if (!el) return;

        if (m.google_key_rejected) {
            el.hidden = false;
            el.textContent = "Google Books refused your API key (" + m.google_key_rejected
                + "). That usually means the Books API is not enabled on that Google "
                + "Cloud project, or the key has API restrictions that exclude it. "
                + "The details above come from Open Library alone.";
        } else if (m.google_quota_exceeded) {
            el.hidden = false;
            el.textContent = "Google Books is rate limiting this key (quota exceeded). "
                + "The details above come from Open Library alone.";
        } else {
            el.hidden = true;
            el.textContent = "";
        }
    }

    // One row per search, by ISBN and by title. Sellers list under both, so a
    // single link would hide whichever half they did not use.
    function renderSearches(links) {
        var box = $("bp-searches");
        if (!box) return;

        var searches = links.searches || [];
        if (!searches.length) { box.innerHTML = ""; return; }

        box.innerHTML = searches.map(function (s) {
            var tag = s.kind === "isbn" ? "by ISBN" : "by title";
            return '<div class="bp-search">' +
                '<span class="bp-search-label">' + tag + ': <em>' +
                escapeHtml(s.label || s.query) + '</em></span>' +
                '<span class="bp-search-links">' +
                '<a href="' + s.active + '" target="_blank" rel="noopener">asking prices →</a>' +
                '<a href="' + s.sold + '" target="_blank" rel="noopener">SOLD prices →</a>' +
                '</span></div>';
        }).join("");
    }

    // The chosen binding carries its own package. Honour the toggle, and never
    // overwrite numbers the seller has typed themselves -- turning a deliberate
    // 2kg book back into 1kg because the format changed would be worse than
    // leaving it stale.
    var packageTouched = false;

    function packageFromFormat(force) {
        var auto = $("bp-package-auto");
        if (auto && !auto.checked && !force) return;
        if (packageTouched && !force) return;

        var select = $("bp-format");
        if (!select || !select.options.length) return;
        var option = select.options[select.selectedIndex];
        if (!option) return;

        var weight = option.dataset.weight;
        if (!weight) return;   // "pick one" has nothing to apply
        setValue("bp-weight", weight);
        setValue("bp-length", option.dataset.length);
        setValue("bp-width", option.dataset.width);
        setValue("bp-height", option.dataset.height);
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
                '<button type="button" class="bp-thumb-x" data-index="' + i + '"'
                + ' title="Remove">×</button>' +
                '<span class="bp-thumb-rotate">' +
                '<button type="button" data-rotate="-90" data-index="' + i + '"'
                + ' title="Turn left">↺</button>' +
                '<button type="button" data-rotate="90" data-index="' + i + '"'
                + ' title="Turn right">↻</button>' +
                '</span></span>';
        }).join("");

        box.querySelectorAll(".bp-thumb-x").forEach(function (btn) {
            btn.addEventListener("click", function () {
                images.splice(Number(btn.dataset.index), 1);
                renderThumbs();
            });
        });
        box.querySelectorAll("[data-rotate]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                rotateImage(Number(btn.dataset.index), Number(btn.dataset.rotate));
            });
        });
    }

    // Rotating a thumbnail with CSS would only turn the preview: the file eBay gets
    // would be unchanged. So the image is turned on a canvas and re-uploaded, which
    // is what actually reaches the listing. EXIF orientation is handled on upload
    // already; this is for the cases it cannot know about -- a book photographed
    // flat, or a cover that is simply the wrong way up.
    function rotateImage(index, degrees) {
        var url = images[index];
        if (!url) return;
        var state = $("bp-photo-status");
        if (state) state.textContent = "Rotating…";

        var image = new Image();
        image.crossOrigin = "anonymous";
        image.onload = function () {
            var quarter = Math.abs(degrees) % 180 === 90;
            var canvas = document.createElement("canvas");
            canvas.width = quarter ? image.naturalHeight : image.naturalWidth;
            canvas.height = quarter ? image.naturalWidth : image.naturalHeight;
            var ctx = canvas.getContext("2d");
            ctx.translate(canvas.width / 2, canvas.height / 2);
            ctx.rotate(degrees * Math.PI / 180);
            ctx.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2);

            canvas.toBlob(function (blob) {
                if (!blob) {
                    if (state) state.textContent = "Could not rotate that photo.";
                    return;
                }
                var body = new FormData();
                body.append("file", blob, "rotated.jpg");
                fetch("/api/upload", { method: "POST", body: body })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data || !(data.url || data.path)) {
                            throw new Error((data && data.error) || "upload failed");
                        }
                        // Replacing the URL rather than editing in place: the browser
                        // caches by URL, so the old address would keep showing the old
                        // orientation.
                        images[index] = data.url || data.path;
                        renderThumbs();
                        if (state) {
                            state.textContent = images.length + " photo"
                                + (images.length === 1 ? "" : "s") + " ready.";
                        }
                    })
                    .catch(function (err) {
                        if (state) state.textContent = "Could not rotate: " + err.message;
                    });
            }, "image/jpeg", 0.92);
        };
        image.onerror = function () {
            if (state) state.textContent = "Could not read that photo to rotate it.";
        };
        // Same origin, so the canvas is not tainted and toBlob is allowed.
        image.src = url;
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
            format: value("bp-format"),
            weight_kg: value("bp-weight"),
            length_cm: value("bp-length"),
            width_cm: value("bp-width"),
            height_cm: value("bp-height"),
            aspects: {
                "Author": value("bp-author") ? [value("bp-author")] : [],
                "Book Title": value("bp-title") ? [value("bp-title")] : [],
                "Publisher": value("bp-publisher") ? [value("bp-publisher")] : [],
                "Publication Year": value("bp-year") ? [value("bp-year")] : [],
                "ISBN": value("bp-isbn-final") ? [value("bp-isbn-final")] : [],
            },
        };
    }

    // Publish immediately. This is the one action on the page that puts something up
    // for sale, so the confirmation carries the price and says outright that a buyer
    // can purchase it. A button that sometimes drafts and sometimes publishes would
    // be the worst of both, which is why this is separate from Create draft.
    function publishNow() {
        var btn = $("bp-publish-btn");
        var result = $("bp-result");
        var title = value("bp-title");
        var price = value("bp-price");
        if (!title) { status("A title is required."); return; }
        if (!parseFloat(price)) { status("A price above zero is required."); return; }

        askToPublish(title, price).then(function (ok) {
            if (!ok) return;
            runPublish(btn, result);
        });
    }

    function runPublish(btn, result) {
        var title = value("bp-title");
        var price = value("bp-price");
        btn.disabled = true;
        btn.textContent = "Publishing\u2026";
        result.hidden = true;

        fetch("/api/books/publish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ draft: draftPayload(), confirm: true }),
        })
            .then(function (r) { return r.json().then(function (d) { return [r, d]; }); })
            .then(function (pair) {
                var res = pair[0], data = pair[1];
                result.hidden = false;
                if (!data.ok) {
                    // A partial outcome is possible: the draft may exist even though
                    // publishing failed. Saying so is what stops the seller hunting
                    // for a listing that was never created.
                    if (data.created) {
                        result.className = "bp-result bp-result--fail";
                        result.innerHTML = data.error
                            + "<br>It is on your "
                            + '<a href="' + (data.draft_url || "/books/drafts") + '">'
                            + "book drafts</a> page.";
                    } else {
                        result.className = "bp-result bp-result--fail";
                        result.textContent = data.error || ("HTTP " + res.status);
                    }
                    return;
                }
                result.className = "bp-result bp-result--ok";
                result.innerHTML = "Live on eBay &mdash; listing <strong>"
                    + data.listing_id + "</strong>." +
                    (data.url ? '<br><a href="' + data.url + '" target="_blank" '
                                + 'rel="noopener">View it on eBay \u2192</a>' : "");
                // Clear the form for the next book, keeping the confirmation.
                stashResult(result.innerHTML, "ok");
                window.setTimeout(function () { window.location.reload(); }, 1200);
            })
            .catch(function (err) {
                result.hidden = false;
                result.className = "bp-result bp-result--fail";
                result.textContent = err.message;
            })
            .then(function () {
                btn.disabled = false;
                btn.textContent = "Publish now \u2014 goes live";
            });
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
                // Points at OUR drafts page. eBay's drafts list does not show
                // offers created through the Inventory API, so sending the seller
                // there showed them a page without their new draft on it, which
                // read as "nothing happened".
                result.innerHTML =
                    "Draft created — offer <strong>" + out.data.offer_id +
                    "</strong> (status " + out.data.status + ").<br>" +
                    "Nothing is on sale yet. " +
                    '<a href="' + out.data.draft_url + '">Open your book drafts →</a>' +
                    "<br><span class=\"bp-hint\">eBay's own drafts page does not list "
                    + "these, so they are kept on the drafts page instead.</span>";
                // It opens too, so the draft is not left to be hunted for. The
                // List a Book tab stays where it is, ready for the next book.
                window.open(out.data.draft_url, "_blank", "noopener");
                // Clearing here too: the next action after a draft is another book,
                // and a form that keeps the last one is how the wrong ISBN ends up on
                // the next listing.
                stashResult(result.innerHTML, "ok");
                window.setTimeout(function () { window.location.reload(); }, 1200);
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

        if ($("bp-scan-btn")) $("bp-scan-btn").addEventListener("click", startScanner);
        if ($("bp-camera-btn")) $("bp-camera-btn").addEventListener("click", startCapture);
        if ($("pc-shutter")) $("pc-shutter").addEventListener("click", snap);
        if ($("pc-done")) $("pc-done").addEventListener("click", finishCapture);
        if ($("pc-cancel")) $("pc-cancel").addEventListener("click", function () {
            // Discarded deliberately: leaving the camera must not upload what was
            // taken after the seller decided against it.
            stopCapture();
            shots = [];
        });
        if ($("bs-shutter")) $("bs-shutter").addEventListener("click", captureFrame);
        if ($("bs-close")) $("bs-close").addEventListener("click", stopScanner);
        // The camera must not keep running behind a closed overlay.
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) { stopScanner(); stopCapture(); }
        });

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
        // The confirmation from before the reload, if there was one.
        showStashedResult();

        if ($("bp-title")) $("bp-title").addEventListener("input", titleCount);

        var format = $("bp-format");
        if (format) {
            format.addEventListener("change", function () {
                // A new binding is an explicit choice, so it reapplies even if the
                // seller had adjusted the numbers by hand.
                packageTouched = false;
                packageFromFormat(true);
            });
        }
        var auto = $("bp-package-auto");
        if (auto) {
            auto.addEventListener("change", function () {
                packageTouched = false;
                if (auto.checked) packageFromFormat(true);
            });
        }
        ["bp-weight", "bp-length", "bp-width", "bp-height"].forEach(function (id) {
            var el = $(id);
            // Only a human edit counts: the pre-fill writes these too.
            if (el) el.addEventListener("input", function () { packageTouched = true; });
        });
        if ($("bp-create-btn")) $("bp-create-btn").addEventListener("click", createDraft);
        if ($("bp-publish-btn")) $("bp-publish-btn").addEventListener("click", publishNow);
    });
})();
