// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { QuotaQueue } from "@/features/portal/QuotaQueue";

export const Route = createFileRoute("/admin/quota-requests")({ component: QuotaQueue });
