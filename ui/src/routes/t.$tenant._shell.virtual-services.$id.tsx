// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { VsDetailLayout } from "@/features/vs/VsDetail";

function Layout() {
  const { tenant, id } = Route.useParams();
  return <VsDetailLayout tenant={tenant} id={id} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id")({
  component: Layout,
});
