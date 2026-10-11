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
