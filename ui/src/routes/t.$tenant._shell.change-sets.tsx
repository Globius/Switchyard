// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { ChangeSets } from "@/features/changesets/ChangeSets";

function Page() {
  const { tenant } = Route.useParams();
  return <ChangeSets tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/change-sets")({ component: Page });
