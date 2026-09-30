// Smoke-runs static/js/dashboard.js against a stubbed DOM.
//
// node --check only proves the file parses. The failure that took the dashboard
// down — a module-level function reaching for `state`, which lives inside the
// DOMContentLoaded handler — is a runtime ReferenceError, invisible to a syntax
// check and to every static test in the suite. This actually runs the handler and
// the render path, so that class of bug cannot reach production again.
//
// Usage: node tests/js_smoke_check.js [path/to/dashboard.js]

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2]
    || path.join(__dirname, "..", "static", "js", "dashboard.js");

function makeElement(id) {
    const listeners = {};
    const element = {
        id,
        hidden: false,
        disabled: false,
        value: "",
        textContent: "",
        innerHTML: "",
        dataset: {},
        style: {},
        children: [],
        classList: {
            _set: new Set(),
            add(...c) { c.forEach(x => this._set.add(x)); },
            remove(...c) { c.forEach(x => this._set.delete(x)); },
            contains(c) { return this._set.has(c); },
            toggle(c) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); },
        },
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener() {},
        setAttribute() {},
        getAttribute() { return null; },
        removeAttribute() {},
        appendChild(c) { this.children.push(c); return c; },
        insertBefore(c) { this.children.push(c); return c; },
        removeChild() {},
        remove() {},
        focus() {},
        blur() {},
        click() {},
        closest() { return null; },
        contains() { return false; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        getBoundingClientRect() {
            return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
        },
        _listeners: listeners,
    };
    return element;
}

const elements = new Map();
function getElement(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
}

const domReady = [];
const windowListeners = {};

const storage = {
    _data: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._data, k) ? this._data[k] : null; },
    setItem(k, v) { this._data[k] = String(v); },
    removeItem(k) { delete this._data[k]; },
};

const documentStub = {
    body: makeElement("body"),
    documentElement: makeElement("html"),
    readyState: "loading",
    getElementById: getElement,
    querySelector: () => null,
    // The view toggle is found by class, so the stub has to answer for it or the
    // table path can never be reached in this harness.
    querySelectorAll: (selector) => {
        if (selector === ".view-toggle-btn") {
            return [
                getElement("view-comfortable"),
                getElement("view-compact"),
                getElement("view-table"),
            ];
        }
        return [];
    },
    createElement: (tag) => makeElement(tag),
    createDocumentFragment: () => makeElement("#fragment"),
    addEventListener(type, fn) { if (type === "DOMContentLoaded") domReady.push(fn); },
    removeEventListener() {},
    cookie: "",
};

const windowStub = {
    document: documentStub,
    localStorage: storage,
    location: { href: "", assign() {}, reload() {} },
    navigator: { userAgent: "node" },
    scrollY: 0,
    innerWidth: 1280,
    innerHeight: 800,
    devicePixelRatio: 1,
    addEventListener(type, fn) { (windowListeners[type] ||= []).push(fn); },
    removeEventListener() {},
    requestAnimationFrame(fn) { return setTimeout(fn, 0); },
    cancelAnimationFrame() {},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    scrollTo() {},
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
};

// A response shaped like GET /api/listings, including a grouped pair so both the
// grouping pass and the table's all-rows path are exercised.
const listingsPayload = {
    total: 2,
    page: 1,
    page_size: 50,
    listings: [
        {
            id: 1, platform: "ebay", platform_listing_id: "a", title: "One",
            price_cents: 1000, total_cost_cents: 200, status: "active",
            currency: "CAD", free_shipping: true, views_count: 5,
            created_at: "2026-01-02T03:04:05",
        },
        {
            id: 2, platform: "etsy", platform_listing_id: "b", title: "Two",
            price_cents: 2000, total_cost_cents: 400, status: "sold",
            is_sold: true, net_payout_cents: 1800, shipping_cost_cents: 100,
            fees_cents: 200, currency: "CAD", sold_at: "2026-02-03T00:00:00",
            group_summary: { id: 9, representative_id: 2, member_count: 2 },
            created_at: "2026-01-03T03:04:05",
        },
    ],
};

function fakeResponse(body) {
    return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
        headers: { get: () => "application/json" },
    };
}

const errors = [];

const sandbox = {
    window: windowStub,
    document: documentStub,
    localStorage: storage,
    navigator: windowStub.navigator,
    location: windowStub.location,
    console: {
        log: () => {},
        warn: (...a) => errors.push("console.warn: " + a.join(" ")),
        error: (...a) => errors.push("console.error: " + a.join(" ")),
    },
    fetch: async (url) => {
        const u = String(url);
        if (u.includes("/api/listings")) return fakeResponse(listingsPayload);
        if (u.includes("/api/stats")) {
            return fakeResponse({ total_listings: 2, timeline: { type: "monthly", points: [] } });
        }
        if (u.includes("/api/teams")) return fakeResponse([]);
        if (u.includes("/api/config")) return fakeResponse({});
        return fakeResponse({ ok: true });
    },
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: windowStub.requestAnimationFrame,
    matchMedia: windowStub.matchMedia,
    URLSearchParams,
    Intl, JSON, Math, Date, Number, String, Boolean, Array, Object, Set, Map,
    Promise, Error, isNaN, parseFloat, parseInt, RegExp,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

// The page loads app.js first (base.html) and dashboard.js second, and the
// dashboard relies on helpers app.js defines — escapeHtml among them. Loading
// only dashboard.js reports those as missing, which is a harness gap rather than
// a real fault.
const scripts = [
    path.join(__dirname, "..", "static", "js", "app.js"),
    target,
];

const context = vm.createContext(sandbox);

for (const file of scripts) {
    let source;
    try {
        source = fs.readFileSync(file, "utf8");
    } catch (err) {
        console.error("FAIL: cannot read " + file + ": " + err.message);
        process.exit(1);
    }
    try {
        vm.runInContext(source, context, { filename: file });
    } catch (err) {
        console.error("FAIL: " + path.basename(file) + " threw while loading: " + err.message);
        process.exit(1);
    }
}

if (!domReady.length) {
    console.error("FAIL: no DOMContentLoaded listener was registered");
    process.exit(1);
}

(async () => {
    // The handler is async in the parts that matter; let its promises settle.
    for (const fn of domReady) {
        try {
            await fn();
        } catch (err) {
            errors.push("DOMContentLoaded handler threw: " + err.message);
        }
    }
    await new Promise(r => setTimeout(r, 60));
    await new Promise(r => setTimeout(r, 60));

    const grid = getElement("listing-grid");
    const body = getElement("listing-table-body");
    const head = getElement("listing-table-head");

    const rendered = String(grid.innerHTML || "");
    if (rendered.includes("Could not load listings")) {
        const m = rendered.match(/Could not load listings: ([^<]*)/);
        errors.push("the listing load failed: " + (m ? m[1] : "unknown"));
    }

    if (errors.length) {
        console.error("FAIL");
        errors.forEach(e => console.error("  - " + e));
        process.exit(1);
    }

    console.log("  cards: loaded, handler ran, " +
        (String(rendered).match(/listing-card/g) || []).length + " card refs rendered");

    // Now the table, which is the path that broke. Switching view must repaint
    // from what is loaded, without a second request.
    const tableButton = getElement("view-table");
    tableButton.dataset.view = "table";
    const clickHandlers = tableButton._listeners.click || [];
    if (!clickHandlers.length) {
        console.error("FAIL: the table toggle has no click handler");
        process.exit(1);
    }
    for (const fn of clickHandlers) {
        try {
            fn({ stopPropagation() {} });
        } catch (err) {
            errors.push("switching to the table threw: " + err.message);
        }
    }
    await new Promise(r => setTimeout(r, 40));

    const tableHeadHtml = String(head.innerHTML || "");
    const tableBodyHtml = String(body.innerHTML || "");
    const thCount = (tableHeadHtml.match(/<th/g) || []).length;
    const trCount = (tableBodyHtml.match(/<tr/g) || []).length;

    if (errors.length) {
        console.error("FAIL");
        errors.forEach(e => console.error("  - " + e));
        process.exit(1);
    }
    if (thCount === 0) {
        console.error("FAIL: switching to the table rendered no column headings");
        process.exit(1);
    }
    if (trCount === 0) {
        console.error("FAIL: switching to the table rendered no rows");
        process.exit(1);
    }
    if (tableBodyHtml.includes("undefined")) {
        console.error("FAIL: a table cell rendered the literal text undefined");
        process.exit(1);
    }

    console.log("  table: " + thCount + " headings, " + trCount + " row(s) rendered");
    console.log("  note: " + String(getElement("listing-table-note").textContent || "").trim());
    process.exit(0);
})();
