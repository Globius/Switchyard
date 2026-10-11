/**
 * Run against the production proxy (deploy/proxy), not the Vite dev server:
 *   PW_BASE_URL=http://localhost:18080 PW_CHANNEL=chrome pnpm e2e csp
 * Fails on any Content-Security-Policy violation, and checks that Monaco mounts under the policy.
 */
import { expect, test } from "./fixtures";

test.skip(!process.env.PW_BASE_URL, "needs the production proxy: set PW_BASE_URL");

const PAGES = [
  "/login",
  "/t/acme",
  "/t/acme/virtual-services/v1/rendered",
  "/t/acme/revisions",
];

for (const path of PAGES)
  test(`no CSP violations on ${path}`, async ({ page }) => {
    const violations: string[] = [];
    page.on("console", (m) => {
      if (/Content Security Policy|Refused to/i.test(m.text())) violations.push(m.text());
    });
    await page.addInitScript(() => {
      document.addEventListener("securitypolicyviolation", (e) =>
        console.error(`Refused to: ${e.violatedDirective} ${e.blockedURI}`),
      );
    });
    await page.route("**/api/v1/tenants/acme/virtual-services/v1/rendered*", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              file: "haproxy/haproxy.cfg",
              fingerprint: "0123456789abcdef0123456789abcdef",
              range: [2, 3],
              content: ["global", "    maxconn 100", "    log /dev/log local0", ""].join("\n"),
            },
          ],
        },
      }),
    );
    const response = await page.goto(path);
    expect(response?.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");
    await page.waitForLoadState("networkidle");
    if (path.endsWith("/rendered")) await expect(page.locator(".monaco-editor").first()).toBeVisible();
    expect(violations).toEqual([]);
  });
