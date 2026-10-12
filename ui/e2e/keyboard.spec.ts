// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { expect, test } from "./fixtures";

test("Ctrl+K opens the command palette and Esc closes it", async ({ page }) => {
  await page.goto("/t/acme/virtual-services");
  await page.getByRole("heading", { name: "Virtual services" }).waitFor();
  await page.keyboard.press("Control+k");
  const input = page.getByPlaceholder(/search|find|jump|type/i).first();
  await expect(input).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(input).toBeHidden();
});

test("the settings form can be edited with the keyboard only, and every Tab stop shows focus", async ({
  page,
}) => {
  await page.goto("/t/acme/virtual-services/v1/settings");
  const name = page.getByLabel("Name", { exact: true });
  await name.waitFor();
  await name.focus();
  await page.keyboard.type("-2");
  await expect(page.getByRole("button", { name: "Save to draft" })).toBeEnabled();
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    // The focused control or a wrapper around it (chip inputs ring their box) shows a ring.
    const ring = await page.evaluate(() => {
      const first = document.activeElement as HTMLElement | null;
      if (!first || first === document.body) return "none";
      let el: HTMLElement | null = first;
      for (let depth = 0; el && depth < 4; depth++, el = el.parentElement) {
        const s = getComputedStyle(el);
        if (s.outlineStyle !== "none" || s.boxShadow !== "none") return "visible";
      }
      return `none on ${first.outerHTML.slice(0, 120)}`;
    });
    expect(ring).toBe("visible");
  }
});

test("switches expose their state and toggle with Space", async ({ page }) => {
  await page.goto("/t/acme/virtual-services/v1/settings");
  const enabled = page.getByRole("switch", { name: "Enabled" });
  await expect(enabled).toHaveAttribute("aria-checked", "true");
  await enabled.focus();
  await page.keyboard.press("Space");
  await expect(enabled).toHaveAttribute("aria-checked", "false");
});
