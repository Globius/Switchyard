// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Jobs } from "@/features/jobs/Jobs";

function Page() {
  const { tenant } = Route.useParams();
  return <Jobs tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/jobs")({ component: Page });
