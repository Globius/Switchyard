import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import { loadEnv } from "vite";
import { defineConfig, type ViteUserConfig } from "vitest/config";

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
      proxy: {
        "/api": {
          target: api,
          headers: devToken ? { Authorization: `Bearer ${devToken}` } : undefined,
        },
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      include: ["tests/**/*.test.{ts,tsx}"],
      testTimeout: 15000,
      setupFiles: ["tests/setup.ts"],
      coverage: {
        provider: "v8",
        include: ["src/lib/**", "src/auth/**", "src/features/**"],
        thresholds: { lines: 80, functions: 80, statements: 80, branches: 80 },
      },
    },
  };
}
