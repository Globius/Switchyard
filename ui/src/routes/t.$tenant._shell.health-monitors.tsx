// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Monitors } from "@/features/monitors/Monitors";

function Page() {
  const { tenant } = Route.useParams();
  return <Monitors tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/health-monitors")({ component: Page });
