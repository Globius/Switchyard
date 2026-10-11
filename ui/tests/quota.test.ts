import { describe, expect, it } from "vitest";
import { problemHint } from "@/features/review/model";

describe("quota problems", () => {
  it("explains a quota problem and says nothing for others", () => {
    expect(problemHint({ code: "quota_exceeded", path: "quota.virtual_service" })).toMatch(
      /limit for virtual service/,
    );
    expect(
      problemHint({ code: "wrong_certificate_purpose", path: "ports.0.client_ca_id" }),
    ).toMatch(/client CA/);
    expect(problemHint({ code: "other", path: "x" })).toBeNull();
  });
});
