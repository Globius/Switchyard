// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Integrations } from "@/features/admin/Integrations";

export const Route = createFileRoute("/admin/integrations")({ component: Integrations });
