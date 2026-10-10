/**
 * Typed API client (openapi-fetch over the generated schema).
 *
 * Auth is a HttpOnly session cookie, so the SPA never sees a token. Every write sends the CSRF
 * token from `GET /me` as `X-CSRF-Token` (docs/design/02-backend-design.md, "Sign-in").
 * A 401 `session_expired` problem calls the registered handler (the re-login modal).
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

let csrfToken: string | null = null;
let onSessionExpired: (() => void) | null = null;

/** Store the token returned by `/me`; cleared on logout. */
export function setCsrfToken(token: string | null | undefined): void {
  csrfToken = token ?? null;
}

/** Register the callback fired when the server reports `session_expired`. */
export function setSessionExpiredHandler(fn: (() => void) | null): void {
  onSessionExpired = fn;
}

/** RFC 9457 problem body, with the stable `code` member the backend adds (G1). */
export interface Problem {
  title: string;
  status: number;
  detail: string;
  code?: string;
  required_role?: string;
  errors?: { path: string; code: string; message: string }[] | null;
}

/** Error thrown by hooks when a request fails; carries the parsed problem when present. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem?: Problem;
  constructor(status: number, problem?: Problem) {
    super(problem?.detail ?? problem?.title ?? `HTTP ${status}`);
    this.status = status;
    this.problem = problem;
  }
}

export const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (!SAFE.has(request.method) && csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      const p = (await response
        .clone()
        .json()
        .catch(() => null)) as Problem | null;
      if (p?.code === "session_expired") onSessionExpired?.();
    }
    return response;
  },
};

/** Build a client; tests pass a `baseUrl` and a custom `fetch`. */
export function createApi(opts: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  const c = createClient<paths>({
    baseUrl: opts.baseUrl ?? globalThis.location?.origin ?? "",
    credentials: "same-origin",
    fetch: opts.fetch ?? ((r) => globalThis.fetch(r)),
  });
  c.use(csrfMiddleware);
  return c;
}

export const api = createApi();

/** Unwrap an openapi-fetch result or throw {@link ApiError}. */
export function unwrap<T>(r: { data?: T; error?: unknown; response: Response }): T {
  if (r.error !== undefined) throw new ApiError(r.response.status, r.error as Problem | undefined);
  return r.data as T;
}
