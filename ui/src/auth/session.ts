// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Session plumbing: `/auth/providers`, `/me`, logout, break-glass login, and a
 * `useCan(tenantId)` hook built on the permission helper. No screens live here.
 */
import { QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { ApiError, api, setCsrfToken, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { createCan } from "@/lib/permissions";

export type Me = components["schemas"]["Me"];
export type AuthProviders = components["schemas"]["AuthProviders"];
export type BreakGlassIn = components["schemas"]["BreakGlassIn"];

export const keys = { me: ["me"] as const, providers: ["auth", "providers"] as const };

/** Query client defaults: never retry 4xx responses (401/403 are answers, not glitches). */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (n, e) => !(e instanceof ApiError && e.status < 500) && n < 2,
        staleTime: 10_000,
      },
    },
  });
}

/** Enabled identity providers for the login page (public). */
export function useProviders() {
  return useQuery({
    queryKey: keys.providers,
    queryFn: async () => unwrap(await api.GET("/api/v1/auth/providers")),
  });
}

/** Current caller, or an `ApiError` with status 401 when signed out. Keeps the CSRF token in sync. */
export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async () => {
      const me = unwrap(await api.GET("/api/v1/me"));
      setCsrfToken(me.csrf_token);
      return me;
    },
  });
}

/** The permission matrix (`GET /permissions`); fetched once, it only changes with a deploy. */
export function usePermissions() {
  return useQuery({
    queryKey: ["permissions"],
    staleTime: Infinity,
    queryFn: async () => unwrap(await api.GET("/api/v1/permissions")),
  });
}

/** `can(verb, object)` for a tenant id; denies everything until `/me` and the matrix have loaded. */
export function useCan(tenantId: string | undefined) {
  const { data: me } = useMe();
  const { data: matrix } = usePermissions();
  return useMemo(() => createCan(me, tenantId, matrix), [me, matrix, tenantId]);
}

/** Tenants the caller can see (`GET /tenants`). */
export function useTenants() {
  return useQuery({
    queryKey: ["tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
  });
}

/** Recompute roles from the current mappings now (`POST /auth/refresh`) and reload `/me`. */
export function useRefreshAccess() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/auth/refresh")),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.me }),
  });
}

/** URL that starts a provider login (a full-page redirect, not a fetch). */
export function loginUrl(providerId: string): string {
  return `/api/v1/auth/login/${encodeURIComponent(providerId)}`;
}

/** Local break-glass login (password + TOTP). */
export function useBreakGlassLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: BreakGlassIn) =>
      unwrap(await api.POST("/api/v1/auth/break-glass", { body })),
    onSuccess: (me) => {
      setCsrfToken(me.csrf_token);
      qc.setQueryData(keys.me, me);
    },
  });
}

/** Ends the session and drops every cached query. */
export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/auth/logout")),
    onSettled: () => {
      setCsrfToken(null);
      qc.clear();
    },
  });
}
