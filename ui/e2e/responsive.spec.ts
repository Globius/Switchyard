import { expect, test } from "./fixtures";

const PAGES = [
  "/t/acme",
  "/t/acme/virtual-services",
  "/t/acme/virtual-services/v1",
  "/t/acme/virtual-services/v1/settings",
  "/t/acme/jobs",
];

for (const path of PAGES)
  test(`phone width: ${path} does not scroll sideways`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    const culprits =
      overflow > 1
        ? await page.evaluate(() => {
            const w = document.documentElement.clientWidth;
            return [...document.querySelectorAll<HTMLElement>("body *")]
              .filter(
                (e) => e.getBoundingClientRect().right > w + 1 && !e.closest("[class*=overflow-x]"),
              )
              .slice(0, 5)
              .map((e) => `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 60)}`);
          })
        : [];
    expect({ overflow, culprits }).toEqual({ overflow: expect.any(Number), culprits: [] });
    expect(overflow).toBeLessThanOrEqual(1);
  });
