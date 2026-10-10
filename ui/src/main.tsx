import { QueryClientProvider } from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { setSessionExpiredHandler } from "@/api/client";
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

// Until the re-login modal is built, an expired session goes back to the login page.
setSessionExpiredHandler(() => void router.navigate({ to: "/login" }));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
);
