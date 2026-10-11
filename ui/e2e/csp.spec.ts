import { expect, test } from "./fixtures";

test("the app runs under its CSP: header present, no violations, Monaco still loads", async ({
  page,
}) => {
  const violations: string[] = [];
  page.on("console", (m) => {
    if (/Content Security Policy|Refused to/i.test(m.text())) violations.push(m.text());
  });
  const res = await page.goto("/t/acme/virtual-services/v1/rendered");
  expect(res?.headers()["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(res?.headers()["x-content-type-options"]).toBe("nosniff");
  await page.waitForLoadState("networkidle");
  expect(violations).toEqual([]);
});
