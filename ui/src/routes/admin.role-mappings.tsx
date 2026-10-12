// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { RoleMappings } from "@/features/admin/RoleMappings";

export const Route = createFileRoute("/admin/role-mappings")({ component: RoleMappings });
