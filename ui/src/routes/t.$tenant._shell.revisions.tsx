// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Revisions } from "@/features/revisions/Revisions";

function Page() {
  const { tenant } = Route.useParams();
  return <Revisions tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/revisions")({ component: Page });
