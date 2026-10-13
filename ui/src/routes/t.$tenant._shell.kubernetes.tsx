// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Kubernetes } from "@/features/kubernetes/Kubernetes";

function Page() {
  const { tenant } = Route.useParams();
  return <Kubernetes tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/kubernetes")({ component: Page });
