// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { CSP, DEV_CSP, securityHeaders } from "@/lib/security";

describe("security headers", () => {
  it("allows only same-origin loads, Monaco workers and styles, and no framing", () => {
    expect(CSP).toContain("default-src 'self'");
    expect(CSP).toContain("script-src 'self'; ");
    expect(CSP).toContain("worker-src 'self' blob:");
    expect(CSP).toContain("frame-ancestors 'none'");
    expect(CSP).toContain("base-uri 'none'");
    expect(CSP).not.toContain("unsafe-eval");
  });

  it("only the dev policy allows inline scripts and the HMR socket", () => {
    expect(DEV_CSP).toContain("script-src 'self' 'unsafe-inline'");
    expect(DEV_CSP).toContain("ws:");
    expect(securityHeaders(false)["Content-Security-Policy"]).toBe(CSP);
    expect(securityHeaders(true)["Content-Security-Policy"]).toBe(DEV_CSP);
    expect(securityHeaders(false)["X-Content-Type-Options"]).toBe("nosniff");
  });
});
