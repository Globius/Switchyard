// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Upgrades } from "@/features/admin/Upgrades";

export const Route = createFileRoute("/admin/upgrades")({ component: Upgrades });
