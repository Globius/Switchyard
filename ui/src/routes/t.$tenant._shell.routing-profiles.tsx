// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { RoutingProfiles } from "@/features/routing/RoutingProfiles";

function Page() {
  const { tenant } = Route.useParams();
  return <RoutingProfiles tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/routing-profiles")({ component: Page });
