/**
 * Screenshots for the README and the docs site, taken against the mocked API with a demo
 * dataset (`e2e/demo.ts`). Skipped unless SY_SCREENSHOTS is set:
 * `SY_SCREENSHOTS=1 pnpm exec playwright test screenshots` writes docs/assets/screenshots/*.png.
 */
import { test } from "./fixtures";
import { mockDemo } from "./demo";

const OUT = "../docs/assets/screenshots";
const SHOTS: [string, string][] = [
  ["dashboard", "/t/acme"],
  ["virtual-services", "/t/acme/virtual-services"],
  ["virtual-service", "/t/acme/virtual-services/v1"],
  ["pools", "/t/acme/pools"],
  ["policies", "/t/acme/policies"],
  ["se-groups", "/t/acme/se-groups"],
  ["certificates", "/t/acme/certificates"],
  ["change-sets", "/t/acme/change-sets"],
  ["review", "/t/acme/review/cs1"],
  ["revisions", "/t/acme/revisions"],
  ["login", "/login"],
];

test.skip(!process.env.SY_SCREENSHOTS, "set SY_SCREENSHOTS=1 to refresh the docs screenshots");
test.use({ viewport: { width: 1440, height: 900 }, colorScheme: "light" });

for (const [name, path] of SHOTS)
  test(`screenshot ${name}`, async ({ page }) => {
    await mockDemo(page);
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    if (name === "review") await page.getByRole("tab", { name: /Config/ }).click();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${OUT}/${name}.png` });
  });
