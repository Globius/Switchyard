// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

/** Sections that are not built yet land here; each gets its own route file as it ships. */
function Section() {
  const { _splat } = Route.useParams();
  return <NotBuilt name={_splat ?? ""} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/$")({ component: Section });
