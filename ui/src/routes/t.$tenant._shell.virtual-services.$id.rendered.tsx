// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { VsRendered } from "@/features/vs/VsRendered";

function Page() {
  const { tenant, id } = Route.useParams();
  return <VsRendered tenant={tenant} id={id} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/rendered")({
  component: Page,
});
