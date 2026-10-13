// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Certificates } from "@/features/certificates/Certificates";

function Page() {
  const { tenant } = Route.useParams();
  return <Certificates tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/certificates")({ component: Page });
