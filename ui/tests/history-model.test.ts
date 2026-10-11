import { describe, expect, it } from "vitest";
import { buildTimeline, changesInRevision, fieldLines, laneOf } from "@/features/vs/history-model";
import { stamp } from "@/features/vs/VsHistory";

describe("history model", () => {
  it("puts approvals and runtime actions in their lanes and skips plain writes", () => {
    expect(laneOf("change_set.reject", "change_set")).toBe("approval");
    expect(laneOf("member.drain", "pool_member")).toBe("runtime");
    expect(laneOf("tenant.update", "tenant")).toBeNull();
  });

  it("sorts newest first with the draft on top, and filters by lane", () => {
    const input = {
      draft: {
        at: "2026-10-11T09:00:00Z",
        title: "2 fields changed",
        author: "me",
        fields: [{ field: "a", before: 1, after: 2 }],
      },
      revisions: [
        { number: 1, summary: "a", author: "x", created_at: "2026-10-01T00:00:00Z" },
        { number: 2, summary: "b", author: "x", created_at: "2026-10-03T00:00:00Z" },
      ],
      audit: [
        { at: "2026-10-02T00:00:00Z", actor: "x", action: "member.drain", object: "pool p" },
        { at: "2026-10-02T00:00:00Z", actor: "x", action: "member.drain", object: "pool p" },
        { at: "2026-10-04T00:00:00Z", actor: "x", action: "change_set.approve", object: "cs" },
      ],
      categories: { "member.drain": "pool_member", "change_set.approve": "change_set" },
    };
    const all = buildTimeline({ ...input, filter: "all" });
    expect(all.map((e) => e.kind)).toEqual([
      "draft",
      "approval",
      "revision",
      "runtime",
      "revision",
    ]);
    expect(buildTimeline({ ...input, filter: "config" }).map((e) => e.kind)).toEqual([
      "draft",
      "revision",
      "revision",
    ]);
    expect(buildTimeline({ ...input, filter: "runtime" })).toHaveLength(1);
    expect(buildTimeline({ ...input, filter: "approvals" })).toHaveLength(1);
  });

  it("writes changed fields as - and + lines and finds one service in two snapshots", () => {
    expect(
      fieldLines([
        { field: "pool", before: "api-v1", after: "api-v2" },
        { field: "new", before: undefined, after: true },
      ]),
    ).toEqual([
      { sign: "-", text: "pool: api-v1" },
      { sign: "+", text: "pool: api-v2" },
      { sign: "+", text: "new: true" },
    ]);
    const a = { virtual_services: [{ id: "v", maxconn: 1 }] };
    const b = {
      virtual_services: [
        { id: "v", maxconn: 2 },
        { id: "w", maxconn: 9 },
      ],
    };
    expect(changesInRevision(a, b, "v")).toEqual([{ field: "maxconn", before: 1, after: 2 }]);
    expect(changesInRevision(undefined, b, "w")).toHaveLength(1);
  });

  it("says today, yesterday or the date", () => {
    const now = new Date("2026-10-11T12:00:00");
    expect(stamp("2026-10-11T10:49:00", now)).toMatch(/^today /);
    expect(stamp("2026-10-10T16:40:00", now)).toMatch(/^yesterday /);
    expect(stamp("2026-10-07T15:20:00", now)).toMatch(/Oct/);
  });
});
