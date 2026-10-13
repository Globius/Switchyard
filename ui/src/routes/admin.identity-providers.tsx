// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { IdentityProviders } from "@/features/admin/IdentityProviders";

export const Route = createFileRoute("/admin/identity-providers")({ component: IdentityProviders });
