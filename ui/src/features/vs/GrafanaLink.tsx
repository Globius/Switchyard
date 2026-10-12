/**
 * "Open in Grafana", the secondary link of the Metrics section (design v52).
 *
 * When the controller proxies Grafana (`links.grafana_embedded`), a click asks for a one-time link
 * scoped to the tenant and opens it in a new tab: the person lands in a Grafana that can read only
 * this tenant's metrics and logs (ADR 0085). Otherwise it is a plain link to `links.grafana`, and
 * with neither it renders nothing.
 */
import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { api, ApiError, unwrap } from "@/api/client";
import { useMe } from "@/auth/session";
import { t } from "@/lib/i18n";

const LINK = "inline-flex items-center gap-1 text-sm text-primary hover:underline";

/** The tenant's dashboard in Grafana, filtered to one service when there is one. */
export function landingFor(service?: string): string {
  return service
    ? `/d/sy-tenant-services?var-service=vs_${encodeURIComponent(service)}`
    : "/d/sy-tenant-services";
}

export function GrafanaLink({ tenant, service }: { tenant: string; service?: string }) {
  const me = useMe();
  const [error, setError] = useState("");
  const links = me.data?.links;

  if (links?.grafana_embedded) {
    const open = async () => {
      setError("");
      // Opened first, inside the click, so a popup blocker allows it; pointed at the link once it exists.
      const tab = window.open("about:blank", "_blank");
      try {
        const launched = unwrap(
          await api.POST("/api/v1/tenants/{t}/grafana/launch", {
            params: { path: { t: tenant } },
            body: { landing: landingFor(service) },
          }),
        );
        if (tab) {
          tab.opener = null;
          tab.location.href = launched.url;
        } else {
          window.location.assign(launched.url);
        }
      } catch (e) {
        tab?.close();
        setError(e instanceof ApiError ? e.message : t("Could not open Grafana."));
      }
    };
    return (
      <span className="inline-flex items-center gap-2">
        <button type="button" className={LINK} onClick={() => void open()}>
          <ExternalLink className="size-3.5" aria-hidden /> {t("Open in Grafana")}
        </button>
        {error && (
          <span role="alert" className="text-xs text-status-down">
            {error}
          </span>
        )}
      </span>
    );
  }
  if (links?.grafana) {
    return (
      <a href={links.grafana} target="_blank" rel="noopener noreferrer" className={LINK}>
        <ExternalLink className="size-3.5" aria-hidden /> {t("Open in Grafana")}
      </a>
    );
  }
  return null;
}
