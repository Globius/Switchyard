// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Tenants } from "@/features/admin/Tenants";

export const Route = createFileRoute("/admin/tenants")({ component: Tenants });
