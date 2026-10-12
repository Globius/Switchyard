// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Gslb } from "@/features/gslb/Gslb";

function Page() {
  const { tenant } = Route.useParams();
  return <Gslb tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/gslb")({ component: Page });
