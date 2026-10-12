// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { SystemSettings } from "@/features/admin/SystemSettings";

export const Route = createFileRoute("/admin/settings")({ component: SystemSettings });
