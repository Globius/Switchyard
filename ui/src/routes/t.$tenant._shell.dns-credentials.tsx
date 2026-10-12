// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { DnsCredentials } from "@/features/dns/DnsCredentials";

function Page() {
  const { tenant } = Route.useParams();
  return <DnsCredentials tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/dns-credentials")({ component: Page });
