// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { describeCreated, describeDeleted } from "@/features/review/model";

const labels = { g1: "edge", p1: "app", vp: "vips" };

describe("describeCreated", () => {
  it("summarises a virtual service with ports, routes and a pool VIP", () => {
    const text = describeCreated(
      "virtual_service",
      {
        protocol: "http",
        se_group_id: "g1",
        vip_pool_id: "vp",
        vip: "10.0.0.5",
        ports: [
          { port: 80, https_redirect_port: 443 },
          { port: 443, tls: true, http2: true },
        ],
        routes: [{}, {}],
        default_pool_id: "p1",
      },
      labels,
    );
    expect(text).toBe(
      "HTTP · edge · VIP from vips (10.0.0.5) · 80 → 443, 443 TLS h2 · 2 routes → app",
    );
    expect(
      describeCreated(
        "virtual_service",
        { protocol: "tcp", ports: [], default_pool_id: "p1" },
        labels,
      ),
    ).toBe("TCP · — · VIP — · → app");
    expect(describeCreated("virtual_service", { protocol: "tcp", routes: [{}] }, labels)).toContain(
      "1 route",
    );
  });

  it("summarises pools, monitors, certificates, policies and other objects", () => {
    expect(
      describeCreated(
        "pool",
        {
          algorithm: "roundrobin",
          members: [{ address: "10.0.0.1", port: 80 }, { address: "10.0.0.2" }],
        },
        labels,
      ),
    ).toBe("roundrobin · 2 members (10.0.0.1:80, 10.0.0.2)");
    expect(describeCreated("pool", { algorithm: "source", members: [] }, labels)).toBe(
      "source · 0 members",
    );
    expect(
      describeCreated("health_monitor", { type: "tcp", interval_s: 5, timeout_s: 2 }, labels),
    ).toBe("TCP · every 5 s, timeout 2 s");
    expect(
      describeCreated("certificate", { source: "acme", sans: ["a.test", "b.test"] }, labels),
    ).toBe("acme · a.test, b.test");
    expect(describeCreated("policy_set", { kind: "redirect" }, labels)).toBe("redirect");
    expect(
      describeCreated(
        "network",
        { id: "x", name: "n", cidr: "10.0.0.0/24", routes: { a: 1 } },
        labels,
      ),
    ).toBe('cidr 10.0.0.0/24 · routes {"a":1}');
  });

  it("says what deleting releases", () => {
    expect(describeDeleted("virtual_service", { vip: "10.0.0.5", vip_pool_id: "vp" }, labels)).toBe(
      "Frees 10.0.0.5 back to vips",
    );
    expect(describeDeleted("virtual_service", { vip: "10.0.0.5", vip_pool_id: "zz" }, labels)).toBe(
      "Frees 10.0.0.5 back to its VIP pool",
    );
    expect(describeDeleted("pool", undefined, labels)).toBe(
      "Removed when the change set is applied.",
    );
  });
});
