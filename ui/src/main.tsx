// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { setSessionExpiredHandler } from "@/api/client";
import { markSessionExpired } from "@/auth/expired";
import { createQueryClient } from "@/auth/session";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "./routeTree.gen";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "@/globals.css";

const queryClient = createQueryClient();
const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

// A 401 session_expired opens the re-login modal over the page, so unsaved input is kept.
setSessionExpiredHandler(() => {
  if (router.state.location.pathname === "/login") return;
  markSessionExpired();
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
