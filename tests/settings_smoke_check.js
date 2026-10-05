// Runs static/js/marketplace-settings.js against a stubbed DOM and API.
//
// Why this exists. The platform panels are built by a loop, and the loader appends
// each panel as it goes. So a ReferenceError inside ONE panel's builder aborts the
// loop and empties the page -- a missing Connect button on one platform took out
// every card after it. That is invisible to `node --check` (the file parses fine)
// and invisible to every test that only greps the source. It shipped once: the
// Connect and Sync buttons were declared with `const` inside an `if`, and the click
// handlers wired further down then threw "connectBtn is not defined" on any
// platform that had no Connect button.
//
// This harness renders the page and counts the panels that survived.
//
// Usage: node tests/settings_smoke_check.js [path/to/marketplace-settings.js]

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const target = process.argv[2]
    || path.join(__dirname, "..", "static", "js", "marketplace-settings.js");

function makeElement(tag) {
    return {
        tagName: tag,
        id: "",
        className: "",
        textContent: "",
        innerHTML: "",
        value: "",
        type: "",
        placeholder: "",
        disabled: false,
        hidden: false,
        style: {},
        dataset: {},
        children: [],
        attributes: {},
        classList: {
            add() {}, remove() {}, contains() { return false; }, toggle() {},
        },
        appendChild(child) { this.children.push(child); return child; },
        removeChild() {},
        setAttribute(key, value) { this.attributes[key] = value; },
        getAttribute(key) { return this.attributes[key] ?? null; },
        addEventListener(type, fn) { (this._listeners ||= {})[type] = fn; },
        removeEventListener() {},
        focus() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        closest() { return null; },
    };
}

const root = makeElement("div");
const statusEl = makeElement("div");
const byId = {
    "marketplace-platforms": root,
    "marketplace-status": statusEl,
    // The account-deletion block is behind a null check, so leaving these out
    // exercises that guard too.
};

// A payload shaped exactly like the API's: two platforms with an OAuth flow, one
// without, one stub, and the API-key-only platform that started all this. The
// non-connectable ones are the ones that used to kill the render.
const CREDENTIALS = {
    ebay: {
        label: "Ebay", connectable: true, is_configured: true,
        fields: [
            { key: "client_id", label: "Client ID", is_set: true, is_secret: false, hint: "" },
            { key: "client_secret", label: "Client Secret", is_set: true, is_secret: true, hint: "\u2022\u2022\u2022\u2022abcd" },
            { key: "ru_name", label: "RuName", is_set: true, is_secret: false, hint: "" },
        ],
    },
    etsy: {
        label: "Etsy", connectable: true, is_configured: true,
        fields: [
            { key: "api_key", label: "Keystring", is_set: true, is_secret: true, hint: "\u2022\u2022\u2022\u2022wxyz" },
            { key: "api_secret", label: "Shared Secret", is_set: true, is_secret: true, hint: "\u2022\u2022\u2022\u20221234" },
        ],
    },
    poshmark: {
        label: "Poshmark", connectable: false, is_configured: false,
        fields: [
            { key: "username", label: "Closet Username", is_set: false, is_secret: false, hint: "" },
            { key: "api_key", label: "API Key", is_set: false, is_secret: true, hint: "" },
        ],
    },
    amazon: {
        label: "Amazon", connectable: false, is_configured: false,
        fields: [
            { key: "seller_id", label: "Seller ID", is_set: false, is_secret: false, hint: "" },
        ],
    },
    google_books: {
        label: "Google Books \u2014 book lookups", connectable: false, is_configured: false,
        fields: [
            { key: "api_key", label: "Google Books API Key", is_set: false, is_secret: true, hint: "" },
        ],
    },
};

const sandbox = {
    console,
    document: {
        getElementById: (id) => byId[id] || null,
        createElement: (tag) => makeElement(tag),
        addEventListener() {},
        querySelector() { return null; },
        querySelectorAll() { return []; },
        body: makeElement("body"),
    },
    fetch: async (url) => {
        const body = url.indexOf("/api/accounts/credentials") !== -1
            ? CREDENTIALS
            : [];
        return {
            ok: true,
            status: 200,
            json: async () => body,
        };
    },
};
sandbox.window = { confirm: () => true, location: { href: "" } };
sandbox.globalThis = sandbox;
sandbox.window.document = sandbox.document;

vm.createContext(sandbox);

const problems = [];

try {
    vm.runInContext(fs.readFileSync(target, "utf8"), sandbox, { filename: target });
} catch (err) {
    problems.push("running the file threw: " + err.message);
}

// The file calls load() at the end of its IIFE, so give the stubbed promises a
// turn to settle before counting what was rendered.
setTimeout(() => {
    const expected = Object.keys(CREDENTIALS).length;
    const rendered = root.children.length;

    if (rendered !== expected) {
        problems.push(
            `rendered ${rendered} of ${expected} platform panels; ` +
            `missing: ${Object.keys(CREDENTIALS).filter((p, i) => i >= rendered).join(", ")}`
        );
    }

    // Every platform must contribute its inputs -- a panel with no fields is the
    // same symptom as a missing panel.
    let inputs = 0;
    root.children.forEach((card) => {
        card.children.forEach((child) => {
            if (child.tagName === "div" && child.children.some(
                    (c) => c.tagName === "input")) {
                inputs += child.children.filter((c) => c.tagName === "input").length;
            } else if (child.tagName === "input") {
                inputs += 1;
            }
        });
    });
    const expectedInputs = Object.values(CREDENTIALS)
        .reduce((total, info) => total + info.fields.length, 0);
    if (inputs !== expectedInputs) {
        problems.push(`rendered ${inputs} input fields, expected ${expectedInputs}`);
    }

    if (problems.length) {
        console.error("FAIL");
        problems.forEach((p) => console.error("  - " + p));
        process.exit(1);
    }
    console.log(`OK: ${rendered} panels, ${inputs} fields, no error`);
}, 50);
