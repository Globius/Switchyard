// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createRootRoute, Outlet } from "@tanstack/react-router";
import { SessionExpiredModal } from "@/features/login/SessionExpiredModal";

export const Route = createRootRoute({
  component: () => (
    <>
      <Outlet />
      <SessionExpiredModal />
    </>
  ),
});
