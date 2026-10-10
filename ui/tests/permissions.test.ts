import { describe, expect, it } from "vitest";
import { createCan, roleAtLeast, roleIn, type PermissionMatrix } from "@/lib/permissions";
import { t } from "@/lib/i18n";

const T1 = "00000000-0000-0000-0000-000000000001";
const T2 = "00000000-0000-0000-0000-000000000002";
const ROLES = ["viewer", "operator", "tenant-admin", "system-admin"] as const;

const MATRIX: PermissionMatrix = {
  roles: [...ROLES],
  permissions: [
    { verb: "read", object: "tenant_data", min_role: "viewer", description: "Read everything." },
    {
      verb: "apply",
      object: "change_set",
      min_role: "operator",
      description: "Apply a change set.",
    },
    { verb: "manage", object: "engine", min_role: "tenant-admin", description: "Manage engines." },
  ],
};

const me = (role: (typeof ROLES)[number], is_system = false) => ({
  is_system,
  tenants: [{ id: T1, role }],
});

describe("permissions", () => {
  it("roles nest in the matrix's order", () => {
    expect(roleAtLeast("tenant-admin", "operator", ROLES)).toBe(true);
    expect(roleAtLeast("viewer", "operator", ROLES)).toBe(false);
    expect(roleAtLeast(null, "viewer", ROLES)).toBe(false);
  });

  it("grants are per tenant; system admins hold system-admin everywhere", () => {
    expect(roleIn(me("operator"), T1)).toBe("operator");
    expect(roleIn(me("operator"), T2)).toBeNull();
    expect(
      roleIn({ is_system: false, tenants: [{ id: T1, name: "acme", role: "viewer" }] }, "acme"),
    ).toBe("viewer");
    expect(roleIn({ is_system: true, tenants: [] }, T2)).toBe("system-admin");
    expect(roleIn(undefined, T1)).toBeNull();
  });

  it.each([
    ["viewer", false, false],
    ["operator", true, false],
    ["tenant-admin", true, true],
  ] as const)("%s: apply=%s engine.manage=%s", (role, apply, manage) => {
    const can = createCan(me(role), T1, MATRIX);
    expect(can("apply", "change_set").allowed).toBe(apply);
    expect(can("manage", "engine").allowed).toBe(manage);
  });

  it("explains a denial with the permission's description", () => {
    const d = createCan(me("viewer"), T1, MATRIX)("apply", "change_set");
    expect(d.reason).toBe("Apply a change set. (needs operator)");
    expect(d.required).toBe("operator");
  });

  it("denies when signed out, before the matrix loads, and for unlisted pairs", () => {
    expect(createCan(undefined, T1, MATRIX)("read", "tenant_data").allowed).toBe(false);
    expect(createCan(me("system-admin"), T1, undefined)("read", "tenant_data").allowed).toBe(false);
    const d = createCan(me("system-admin"), T1, MATRIX)("deploy", "se_group");
    expect(d).toEqual({ allowed: false, required: null });
  });
});

describe("t", () => {
  it("interpolates and leaves unknown vars", () => {
    expect(t("Hi")).toBe("Hi");
    expect(t("{n} of {m}", { n: 2 })).toBe("2 of {m}");
  });
});
