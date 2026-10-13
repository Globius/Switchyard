// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { AdminController } from "@/features/admin/AdminController";

export const Route = createFileRoute("/admin/controller")({ component: AdminController });
