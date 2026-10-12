// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { SeGroups } from "@/features/segroups/SeGroups";

function Page() {
  const { tenant } = Route.useParams();
  return <SeGroups tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/se-groups")({ component: Page });
