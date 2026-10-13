// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Policies } from "@/features/policies/Policies";

function Page() {
  const { tenant } = Route.useParams();
  return <Policies tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/policies")({ component: Page });
