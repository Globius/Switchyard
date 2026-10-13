// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { AdminShell } from "@/features/admin/AdminShell";

export const Route = createFileRoute("/admin")({ component: AdminShell });
