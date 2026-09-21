/* Headless-Chromium UI smoke test for the marketplace dashboard.
   Run:  NODE_PATH=<playwright npx cache>/node_modules node ui_smoke_test.js
   Drives a real browser: login, dashboard, admin teams page; reports console errors. */
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://localhost:8000";

function adminPassword() {
  const env = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
  for (const line of env.split(/\r?\n/)) {
    const m = line.match(/^ADMIN_PASSWORD=(.*)$/);
    if (m && m[1]) return m[1].trim().replace(/^["']|["']$/g, "");
  }
  throw new Error("ADMIN_PASSWORD not found in .env");
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "  [" + detail + "]" : ""));
}

(async () => {
  const pass = adminPassword();
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

  // 1 — login page
  await page.goto(BASE + "/login", { waitUntil: "networkidle" });
  check("login page renders", (await page.title()).trim().length > 0, await page.title());
  const googleBtn = await page.locator('a[href*="google"], button:has-text("Google")').count();
  check("Google sign-in button present", googleBtn > 0);

  // 2 — sign in as admin
  await page.fill('input[name="username"]', "admin");
  await page.fill('input[name="password"]', pass);
  await Promise.all([
    page.waitForURL("**/dashboard", { timeout: 15000 }),
    page.click('form button[type="submit"], form input[type="submit"]'),
  ]);
  check("admin login lands on /dashboard", page.url().includes("/dashboard"), page.url());

  // 3 — dashboard as admin
  const dashBody = await page.locator("body").innerText();
  check("dashboard shows admin identity", /Admin/.test(dashBody));
  check("team filter present", (await page.locator("#filter-team").count()) > 0);
  await page.screenshot({ path: path.join(ROOT, "docs", "images", "ui_smoke_dashboard.png") });

  // 4 — admin page + Teams section
  await page.goto(BASE + "/admin", { waitUntil: "networkidle" });
  const adminBody = await page.locator("body").innerText();
  check("admin page loads with identity", /Admin/.test(adminBody));
  check("Teams section rendered", /Teams/i.test(adminBody));
  check("team create controls present",
    (await page.locator("#create-team").count()) > 0 && (await page.locator("#new-team-name").count()) > 0);
  await page.screenshot({ path: path.join(ROOT, "docs", "images", "ui_smoke_admin.png"), fullPage: true });

  // 5 — JS runtime health
  const realErrors = errors.filter((e) => !/favicon/i.test(e));
  check("no JS console errors", realErrors.length === 0, realErrors.slice(0, 3).join(" | "));

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log("\n" + (results.length - failed.length) + "/" + results.length + " checks passed");
  process.exit(failed.length ? 1 : 0);
})().catch((e) => {
  console.error("SCRIPT ERROR:", e.message);
  process.exit(2);
});
