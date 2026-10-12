// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Skeleton({ className, ...p }: ComponentProps<"div">) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...p} />;
}
