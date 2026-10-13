import { createFileRoute } from "@tanstack/react-router";
import { VirtualServicesPage } from "@/features/vs/VirtualServicesPage";
import type { VsFilters } from "@/features/vs/queries";

/** Filters live in the URL so every view is linkable. */
function parseSearch(s: Record<string, unknown>): VsFilters {
  const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return {
    q: str(s.q),
    sort: s.sort === "-name" ? "-name" : s.sort === "name" ? "name" : undefined,
    se_group: str(s.se_group),
    label: str(s.label),
    protocol:
      s.protocol === "http" || s.protocol === "tcp" || s.protocol === "udp"
        ? s.protocol
        : undefined,
    enabled:
      s.enabled === true || s.enabled === "true"
        ? true
        : s.enabled === false || s.enabled === "false"
          ? false
          : undefined,
  };
}

function Page() {
  const { tenant } = Route.useParams();
  const filters = Route.useSearch();
  return <VirtualServicesPage tenant={tenant} filters={filters} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/")({
  validateSearch: parseSearch,
  component: Page,
});
