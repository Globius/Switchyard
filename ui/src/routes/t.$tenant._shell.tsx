import { createFileRoute } from "@tanstack/react-router";
import { useMe } from "@/auth/session";
import { Shell } from "@/features/shell/Shell";

function ShellRoute() {
  const { tenant } = Route.useParams();
  const { data: me } = useMe();
  return <Shell tenant={tenant} isSystem={me?.is_system ?? false} />;
}

/** Pathless layout: pages inside it get the top bar, nav and change-set bar. */
export const Route = createFileRoute("/t/$tenant/_shell")({ component: ShellRoute });
