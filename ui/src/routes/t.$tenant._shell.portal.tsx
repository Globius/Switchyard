// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Portal } from "@/features/portal/Portal";

function Page() {
  const { tenant } = Route.useParams();
  return <Portal tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/portal")({ component: Page });
