import { describe, expect, it } from "vitest";
import {
  objectLabel,
  principalName,
  shortDate,
  shortRequestId,
  shortWhen,
} from "../src/lib/format";

describe("format", () => {
  it("names principals", () => {
    expect(principalName("user:authentik:dana")).toBe("dana");
    expect(principalName("user:local:breakglass")).toBe("Break-glass");
    expect(principalName(null)).toBe("—");
  });
  it("shortens request ids", () => {
    expect(shortRequestId("req_8d21f00a0f")).toBe("req_8d21…a0f");
    expect(shortRequestId("abc")).toBe("req_abc");
  });
  it("labels objects", () => {
    expect(objectLabel("change_set:01a1285d-aaaa-bbbb")).toBe("Change set 01a1285d");
    expect(objectLabel("pool:web")).toBe("Pool web");
    expect(objectLabel("weird")).toBe("weird");
  });
  it("formats dates", () => {
    const now = new Date(2026, 9, 10, 12, 0);
    expect(shortWhen(new Date(2026, 9, 10, 8, 12).toISOString(), now)).toBe("today 08:12");
    expect(shortWhen(new Date(2026, 9, 9, 22, 40).toISOString(), now)).toBe("yesterday 22:40");
    expect(shortWhen(new Date(2026, 9, 6, 14, 2).toISOString(), now)).toBe("Oct 6 14:02");
    expect(shortDate(new Date(2026, 9, 22, 12).toISOString())).toBe("Oct 22, 2026");
  });
});
