// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Rolls } from "@/features/segroups/Rolls";

function Page() {
  const { tenant } = Route.useParams();
  return <Rolls tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/rolls")({ component: Page });
