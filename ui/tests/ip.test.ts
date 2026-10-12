// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { cidrProblem, compareIp, hostPort, isIPv4, isIPv6, isIp } from "@/lib/ip";

describe("ip helpers", () => {
  it("validates IPv4", () => {
    for (const ok of ["0.0.0.0", "10.40.12.6", "255.255.255.255"]) expect(isIPv4(ok)).toBe(true);
    for (const bad of ["", "1.2.3", "1.2.3.4.5", "256.1.1.1", "01.2.3.4", "a.b.c.d", "1.2.3.4/24"])
      expect(isIPv4(bad)).toBe(false);
  });

  it("validates IPv6, including compression and an IPv4 tail", () => {
    for (const ok of [
      "::",
      "::1",
      "2001:db8::1",
      "2001:0db8:0000:0000:0000:0000:0000:0001",
      "fe80::1:2",
      "::ffff:10.0.0.1",
      "1:2:3:4:5:6:7::",
      "1:2:3:4:5:6:10.0.0.1",
    ])
      expect(isIPv6(ok), ok).toBe(true);
    for (const bad of [
      "",
      ":::",
      "1::2::3",
      "1:2:3:4:5:6:7",
      "1:2:3:4:5:6:7:8:9",
      "12345::1",
      "g::1",
      "2001:db8::1%eth0",
      "[::1]",
      "1.2.3.4",
      "::1.2.3.4:5",
      "1:2:3:4:5:6:7::8",
    ])
      expect(isIPv6(bad), bad).toBe(false);
    expect(isIp("::1") && isIp("1.2.3.4") && !isIp("nope")).toBe(true);
  });

  it("checks the prefix length per family", () => {
    expect(cidrProblem("10.0.0.0/24")).toBeNull();
    expect(cidrProblem("10.0.0.0/33")).toBe("prefix");
    expect(cidrProblem("2001:db8::/64")).toBeNull();
    expect(cidrProblem("2001:db8::/128")).toBeNull();
    expect(cidrProblem("2001:db8::/129")).toBe("prefix");
    expect(cidrProblem("2001:db8::1")).toBeNull();
    expect(cidrProblem("nope/24")).toBe("address");
    expect(cidrProblem("10.0.0.0/2/4")).toBe("address");
    expect(cidrProblem("10.0.0.0/x")).toBe("address");
  });

  it("brackets IPv6 hosts in host:port", () => {
    expect(hostPort("10.0.0.1", 80)).toBe("10.0.0.1:80");
    expect(hostPort("2001:db8::1", 443)).toBe("[2001:db8::1]:443");
    expect(hostPort("[2001:db8::1]", 443)).toBe("[2001:db8::1]:443");
    expect(hostPort("2001:db8::1")).toBe("2001:db8::1");
    expect(hostPort("web.example", null)).toBe("web.example");
  });

  it("orders addresses numerically, IPv4 before IPv6", () => {
    const sorted = [
      "2001:db8::10",
      "10.0.0.10",
      "2001:db8::2",
      "10.0.0.9",
      "::ffff:10.0.0.1",
      "::1",
      "zzz",
    ].sort(compareIp);
    expect(sorted).toEqual([
      "10.0.0.9",
      "10.0.0.10",
      "::1",
      "::ffff:10.0.0.1",
      "2001:db8::2",
      "2001:db8::10",
      "zzz",
    ]);
  });
});
