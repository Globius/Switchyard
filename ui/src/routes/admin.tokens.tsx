// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { SystemTokens } from "@/features/admin/SystemTokens";

export const Route = createFileRoute("/admin/tokens")({ component: SystemTokens });
