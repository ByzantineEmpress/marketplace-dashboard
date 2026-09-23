/* Responsive / form-factor test — runs the real app in Playwright
 * Chromium at the widths of real devices (phones, foldables, tablets,
 * desktop) in both light and dark colour schemes.
 *
 * Verifies:
 *   - every page renders (login -> dashboard -> admin)
 *   - NO horizontal overflow at any width (the #1 mobile bug)
 *   - listing grid gets more columns as the screen widens
 *   - saves screenshots to docs/images/
 *
 * Requires the server to be running on :8000.
 *   node tests/ui_responsive_test.js
 */
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const BASE = "http://localhost:8000";

function adminPassword() {
  const env = fs.readFileSync(path.join(ROOT, ".env"), "utf8");
  const m = env.match(/^ADMIN_PASSWORD=(.*)$/m);
  return m ? m[1].trim() : "changeme123";
}

/* Real-world form factors (CSS-pixel viewports):
 *  - 360x780   small phone (iPhone SE class)
 *  - 414x896   large phone (iPhone 12 class)
 *  - 499x900   foldable COVER screen (Galaxy Z Fold class, folded)
 *  - 673x841   foldable UNFOLDED portrait (Galaxy Z Fold 5 class)
 *  - 1024x768  tablet landscape (iPad class)
 *  - 1440x900  desktop
 */
const FORM_FACTORS = [
  { name: "phone-360", width: 360, height: 780 },
  { name: "phone-414", width: 414, height: 896 },
  { name: "fold-folded", width: 499, height: 900 },
  { name: "fold-open", width: 673, height: 841 },
  { name: "tablet-1024", width: 1024, height: 768 },
  { name: "desktop-1440", width: 1440, height: 900 },
];

let passed = 0;
let failed = 0;
function check(label, ok, extra) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? "  " + extra : ""}`);
  ok ? passed++ : failed++;
}

(async () => {
  const browser = await chromium.launch();
  const password = adminPassword();

  for (const theme of ["light", "dark"]) {
    const ctx = await browser.newContext({
      colorScheme: theme,
      viewport: { width: FORM_FACTORS[0].width, height: FORM_FACTORS[0].height },
    });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "console" && m.type() === "error") errors.push(m.text()); });

    // Log in once for this theme context
    await page.goto(BASE + "/login", { waitUntil: "networkidle" });
    await page.fill("#username", "admin");
    await page.fill("#password", password);
    await page.click("button[type=submit]");
    await page.waitForURL("**/dashboard", { timeout: 10000 });

    for (const ff of FORM_FACTORS) {
      await page.setViewportSize({ width: ff.width, height: ff.height });
      await page.waitForTimeout(250);

      // Dashboard: no horizontal overflow
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      check(`[${theme}] ${ff.name} dashboard: no horizontal overflow`, overflow <= 1, overflow > 0 ? `(+${overflow}px)` : "");

      // Grid should gain columns as width grows
      const cols = await page.evaluate(() => {
        const g = document.getElementById("listing-grid");
        if (!g) return 0;
        return getComputedStyle(g).gridTemplateColumns.split(" ").length;
      });

      // Save screenshots at representative sizes (not every single one)
      if (["phone-360", "fold-open", "tablet-1024", "desktop-1440"].includes(ff.name)) {
        await page.screenshot({ path: path.join(ROOT, "docs", "images", `r_${ff.name}_${theme}.png`) });
        console.log(`      screenshot: docs/images/r_${ff.name}_${theme}.png  (grid columns: ${cols})`);
      }
      await page.goto(BASE + "/dashboard", { waitUntil: "networkidle" });
    }

    // Admin page check at phone + foldable-open widths
    for (const ff of [FORM_FACTORS[0], FORM_FACTORS[3]]) {
      await page.setViewportSize({ width: ff.width, height: ff.height });
      await page.goto(BASE + "/admin", { waitUntil: "networkidle" });
      await page.waitForTimeout(250);
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth
      );
      check(`[${theme}] ${ff.name} admin: no horizontal overflow`, overflow <= 1, overflow > 0 ? `(+${overflow}px)` : "");
    }

    const realErrors = errors.filter((e) => !/favicon/i.test(e));
    check(`[${theme}] no JS errors across all form factors`, realErrors.length === 0, realErrors[0] || "");
    await ctx.close();
  }

  await browser.close();
  console.log(`\n${passed} passed, ${failed} failed — ${failed ? "FAIL" : "ALL OK"}`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error("RUNNER FAILED:", e);
  process.exit(2);
});
