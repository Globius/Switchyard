// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Users } from "@/features/admin/Users";

export const Route = createFileRoute("/admin/users")({ component: Users });
