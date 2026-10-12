// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { AuditLog } from "@/features/admin/AuditLog";

export const Route = createFileRoute("/admin/audit-log")({ component: AuditLog });
