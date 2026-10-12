import { expect, test } from "./fixtures";

for (const width of [1024, 1280, 1440])
  test(`policies fit at ${width}px: every kind is reachable and nothing is clipped`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/t/acme/policies");
    await page.waitForLoadState("networkidle");
    const group = page.getByRole("group", { name: "Kind" });
    for (const name of ["All", "Allow list", "Fixed response", "Login", "Bot and DDoS protection"])
      await expect(group.getByRole("button", { name, exact: true })).toBeVisible();
    await page.getByLabel("New security policy").selectOption("access");
    await expect(page.getByLabel("Issuer URL")).toBeVisible();
    const box = await page.getByLabel("Client secret reference").boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(width);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
    const clipped = await page.evaluate(() => {
      const w = document.documentElement.clientWidth;
      return [...document.querySelectorAll<HTMLElement>("main *")]
        .filter((e) => e.getBoundingClientRect().right > w + 1 && !e.closest("[class*=overflow-x]"))
        .slice(0, 5)
        .map((e) => `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 50)}`);
    });
    expect(clipped).toEqual([]);
  });
