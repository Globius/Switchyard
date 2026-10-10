import { describe, expect, it } from "vitest";
import { createCan, DEFAULT_MATRIX, roleAtLeast, roleIn } from "@/lib/permissions";
import { t } from "@/lib/i18n";

const T1 = "00000000-0000-0000-0000-000000000001";
const T2 = "00000000-0000-0000-0000-000000000002";
const me = (role: "viewer" | "operator" | "tenant-admin" | "system-admin", is_system = false) => ({
  is_system,
  tenants: [{ id: T1, name: "acme", role }],
});

describe("permissions", () => {
  it("roles nest", () => {
    expect(roleAtLeast("tenant-admin", "operator")).toBe(true);
    expect(roleAtLeast("viewer", "operator")).toBe(false);
    expect(roleAtLeast(null, "viewer")).toBe(false);
  });

  it("grants are per tenant; system admins hold system-admin everywhere", () => {
    expect(roleIn(me("operator"), T1)).toBe("operator");
    expect(roleIn(me("operator"), T2)).toBeNull();
    expect(roleIn({ is_system: true, tenants: [] }, T2)).toBe("system-admin");
    expect(roleIn(undefined, T1)).toBeNull();
  });

  // Rows of the matrix in docs/design/02-backend-design.md, per role.
  const cases: [string, "viewer" | "operator" | "tenant-admin" | "system-admin"][] = [
    ["read", "viewer"],
    ["app.write", "operator"],
    ["infra.write", "tenant-admin"],
    ["approval.request", "operator"],
    ["approval.decide", "tenant-admin"],
    ["apply", "operator"],
    ["apply.break_glass", "system-admin"],
    ["rollback", "tenant-admin"],
    ["engine.manage", "tenant-admin"],
    ["require_approval.disable", "system-admin"],
    ["token.manage", "tenant-admin"],
  ];
  it.each(cases)("%s needs %s", (action, min) => {
    const order = ["viewer", "operator", "tenant-admin", "system-admin"] as const;
    for (const role of order) {
      const d = createCan(me(role), T1)(action as keyof typeof DEFAULT_MATRIX);
      expect(d.allowed).toBe(order.indexOf(role) >= order.indexOf(min));
    }
  });

  it("explains a denial and denies when signed out", () => {
    expect(createCan(me("viewer"), T1)("apply").reason).toBe("Requires the operator role");
    expect(createCan(undefined, T1)("read").allowed).toBe(false);
  });

  it("uses a supplied matrix and treats unknown actions as system-only", () => {
    const can = createCan(me("viewer"), T1, { apply: "viewer" });
    expect(can("apply").allowed).toBe(true);
    expect(can("read").required).toBe("system-admin");
  });
});

describe("t", () => {
  it("interpolates and leaves unknown vars", () => {
    expect(t("Hi")).toBe("Hi");
    expect(t("{n} of {m}", { n: 2 })).toBe("2 of {m}");
  });
});
