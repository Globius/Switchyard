// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { AcmeProviders } from "@/features/admin/AcmeProviders";

export const Route = createFileRoute("/admin/acme-providers")({ component: AcmeProviders });
