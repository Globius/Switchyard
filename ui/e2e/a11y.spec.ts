import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./fixtures";

const PAGES = [
  ["login", "/login"],
  ["tenant home", "/t/acme"],
  ["virtual services", "/t/acme/virtual-services"],
  ["virtual service overview with metrics", "/t/acme/virtual-services/v1"],
  ["virtual service settings", "/t/acme/virtual-services/v1/settings"],
  ["virtual service rendered config", "/t/acme/virtual-services/v1/rendered"],
  ["usage and requests", "/t/acme/portal"],
  ["jobs", "/t/acme/jobs"],
  ["revisions", "/t/acme/revisions"],
] as const;

for (const [name, path] of PAGES)
  test(`axe: ${name} has no serious violations`, async ({ page }) => {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(
      bad.map(
        (v) =>
          `${v.id}: ${v.nodes.length} nodes, e.g. ${v.nodes[0]?.target.join(" ")} ${v.nodes[0]?.html.slice(0, 160)} ${v.nodes[0]?.any[0]?.message ?? ""}`,
      ),
    ).toEqual([]);
  });
