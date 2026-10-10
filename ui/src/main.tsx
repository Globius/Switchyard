import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createQueryClient } from "@/auth/session";
import { t } from "@/lib/i18n";
import "@/styles/globals.css";

const queryClient = createQueryClient();

// Screens are built from the Claude Design mockups; until then the app is groundwork only.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <main className="p-6">{t("Switchyard")}</main>
    </QueryClientProvider>
  </StrictMode>,
);
