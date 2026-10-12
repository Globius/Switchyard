import { createFileRoute } from "@tanstack/react-router";
import { Alerts } from "@/features/alerts/Alerts";

function Page() {
  const { tenant } = Route.useParams();
  const { new: add } = Route.useSearch();
  return <Alerts tenant={tenant} startWith={add === "rule" ? "rule" : null} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/alerts")({
  component: Page,
  validateSearch: (s: Record<string, unknown>): { new?: string } =>
    typeof s.new === "string" ? { new: s.new } : {},
});
