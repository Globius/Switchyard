// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Review } from "@/features/review/Review";

// Full-screen review and apply (no shell).
function Page() {
  const { tenant, cs } = Route.useParams();
  return <Review tenant={tenant} cs={cs} />;
}

export const Route = createFileRoute("/t/$tenant/review/$cs")({ component: Page });
