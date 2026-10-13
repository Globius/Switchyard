// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import { loadEnv } from "vite";
import { defineConfig, type ViteUserConfig } from "vitest/config";
import { securityHeaders } from "./src/lib/security";

// Dev only. SY_API is the controller (default http://localhost:8000). SY_DEV_TOKEN, when set in
// ui/.env.local, is sent as a bearer token so you can use the UI before the login screen exists.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "SY_");
  return config(env.SY_API ?? "http://localhost:8000", env.SY_DEV_TOKEN);
});

function config(api: string, devToken?: string): ViteUserConfig {
  return {
    plugins: [tanstackRouter({ target: "react", autoCodeSplitting: true }), react(), tailwindcss()],
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    server: {
      headers: securityHeaders(true),
      proxy: {
        "/api": {
          target: api,
          headers: devToken ? { Authorization: `Bearer ${devToken}` } : undefined,
        },
      },
    },
    preview: { headers: securityHeaders(false) },
    test: {
      environment: "jsdom",
      globals: true,
      include: ["tests/**/*.test.{ts,tsx}"],
      // The CI runners are small shared pods: 49 jsdom workers at once starved each other (about 17 s to start
      // one, so tests hit the 15 s limit with no real failure). Fewer workers, longer limits there.
      testTimeout: process.env.CI ? 60000 : 15000,
      hookTimeout: process.env.CI ? 60000 : 10000,
      ...(process.env.CI ? { maxWorkers: 2 } : {}),
      setupFiles: ["tests/setup.ts"],
      coverage: {
        provider: "v8",
        include: ["src/lib/**", "src/auth/**", "src/features/**"],
        // The Monaco loader only runs in a real browser; the Playwright checks exercise it.
        exclude: ["src/lib/monaco.ts"],
        thresholds: { lines: 80, functions: 80, statements: 80, branches: 80 },
      },
    },
  };
}
