import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { describeRoute, isListField, ListDiff } from "@/features/review/ListDiff";
import { diffKeyed, diffOrdered, mergeAdds, memberKey, onlyAdds, routeKey } from "@/lib/list-diff";

const r = (host: string, pool?: string, path?: string, kind: "prefix" | "exact" = "prefix") => ({
  match: {
    host,
    path_prefix: kind === "prefix" ? (path ?? null) : null,
    path_exact: kind === "exact" ? (path ?? null) : null,
  },
  pool_id: pool ?? "p1",
  redirect: null,
});

describe("ordered diff", () => {
  it("marks added, removed, changed in place and moved rows with positions from 1", () => {
    const before = [r("a"), r("b"), r("c")];
    const after = [r("c"), r("a", "p2"), r("d")];
    const rows = diffOrdered(before, after, routeKey);
    expect(rows.map((x) => [x.marker, x.position])).toEqual([
      ["↕", 1], // c moved before a
      ["~", 2], // a: target changed
      ["+", 3], // d added
      ["−", 2], // b removed (live position)
    ]);
  });
  it("leaves matching rows unmarked and matches by host, path and match type", () => {
    const same = [r("a", "p1", "/x"), r("a", "p1", "/x", "exact")];
    expect(diffOrdered(same, same, routeKey).every((x) => x.marker === " ")).toBe(true);
    expect(routeKey(r("a", "p", "/x"))).not.toBe(routeKey(r("a", "p", "/x", "exact")));
    expect(routeKey({ match: {} })).toBe("* any");
  });
});

describe("keyed diff", () => {
  it("collapses unchanged rows", () => {
    const m = (name: string, port: number, weight = 100) => ({
      name,
      address: "10.0.0.1",
      port,
      weight,
    });
    const d = diffKeyed(
      [m("a", 80), m("b", 80), m("c", 80)],
      [m("a", 80), m("b", 80, 50), m("d", 80)],
      memberKey,
    );
    expect(d.unchanged).toBe(1);
    expect(d.rows.map((x) => [x.marker, x.key])).toEqual([
      ["~", "b:80"],
      ["+", "d:80"],
      ["−", "c:80"],
    ]);
  });
  it("detects when each side only added items and merges theirs first", () => {
    const k = (s: string) => s;
    expect(onlyAdds(["a"], ["a", "b"], k)).toBe(true);
    expect(onlyAdds(["a", "b"], ["a"], k)).toBe(false);
    expect(mergeAdds(["a"], ["a", "t"], ["a", "y"], k)).toEqual(["a", "t", "y"]);
    expect(mergeAdds(["a"], ["a", "t"], ["a", "t", "y"], k)).toEqual(["a", "t", "y"]);
    expect(mergeAdds(["a", "b"], ["a"], ["a", "b", "y"], k)).toBeNull();
    expect(
      mergeAdds([{ id: "a", v: 1 }], [{ id: "a", v: 2 }], [{ id: "a", v: 1 }], (x) => x.id),
    ).toBeNull();
  });
});

describe("ListDiff", () => {
  const labels = { p1: "api-v1", p2: "api-v2", c1: "wildcard", c2: "portal" };
  it("recognises list fields", () => {
    expect(isListField("routes", [], [])).toBe(true);
    expect(isListField("name", "a", "b")).toBe(false);
    expect(isListField("routes", null, undefined)).toBe(false);
  });
  it("renders routes as a summary and a numbered Live/Draft table", () => {
    render(
      <ListDiff
        field="routes"
        revision={41}
        before={[r("a.test", "p1"), r("b.test", "p1")]}
        after={[
          r("b.test", "p1"),
          r("a.test", "p2"),
          { match: { host: "old.test" }, redirect: { location: "https://new.test", code: 301 } },
        ]}
        labels={labels}
      />,
    );
    expect(
      screen.getByText("2 routes → 3 routes · 1 added · 1 changed · 1 moved"),
    ).toBeInTheDocument();
    expect(screen.getByText("Live (rev #41)")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows.map((x) => x.getAttribute("data-marker"))).toEqual(["↕", "~", "+"]);
    expect(within(rows[0]!).getByText("↕ moved")).toBeInTheDocument();
    expect(within(rows[1]!).getByText(/a.test → api-v1/)).toHaveClass("line-through");
    expect(within(rows[1]!).getByText(/a.test → api-v2/)).toBeInTheDocument();
    expect(within(rows[2]!).getByText(/redirect 301 https:\/\/new.test/)).toBeInTheDocument();
    expect(screen.getByText(/Matched by host, path and match type/)).toBeInTheDocument();
  });
  it("renders members and ports keyed, hiding unchanged rows behind a toggle", async () => {
    const { unmount } = render(
      <ListDiff
        field="members"
        before={[
          { name: "a", address: "10.0.0.1", port: 80, weight: 1 },
          { name: "b", address: "10.0.0.2", port: 80, weight: 1 },
        ]}
        after={[
          { name: "a", address: "10.0.0.1", port: 80, weight: 1 },
          { name: "c", address: "10.0.0.3", port: 80, weight: 1 },
        ]}
        labels={labels}
      />,
    );
    expect(
      screen.getByText("2 members → 2 members · 1 added · 1 removed · 1 unchanged (hidden)"),
    ).toBeInTheDocument();
    expect(screen.getByText(/10.0.0.3:80 · weight 1 · enabled/)).toBeInTheDocument();
    expect(screen.getByText("+ added")).toBeInTheDocument();
    expect(screen.getByText("− removed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show 1 unchanged" }));
    expect(screen.getByRole("button", { name: "Hide unchanged" })).toBeInTheDocument();
    unmount();
    const p = render(
      <ListDiff
        field="ports"
        before={[{ port: 80 }]}
        after={[{ port: 80 }, { port: 443, tls: true, http2: true }]}
        labels={labels}
      />,
    );
    expect(screen.getByText("443 TLS h2")).toBeInTheDocument();
    p.unmount();
    render(
      <ListDiff field="certificate_ids" before={["c1"]} after={["c1", "c2"]} labels={labels} />,
    );
    expect(screen.getAllByText("portal").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Show 1 unchanged" })).toBeInTheDocument();
  });
  it("describes a route without a host", () => {
    expect(describeRoute({ match: { path_exact: "/x" }, pool_id: "p1" }, labels)).toBe(
      "exact /x → api-v1",
    );
    expect(describeRoute({ match: {} }, {})).toBe("any request → —");
  });
});
