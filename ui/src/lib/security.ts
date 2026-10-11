/**
 * Security headers for the dev and preview servers, so a CSP breakage shows up while developing.
 * Production serving (a reverse proxy) should send the same policy; see docs/security/ui-review.md.
 */

/** The policy for a built app. Monaco needs blob workers and inline styles; everything else is same-origin. */
export const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "worker-src 'self' blob:",
  "img-src 'self' data:",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join("; ");

/** The dev server also needs the Vite client's inline preamble and its HMR WebSocket. */
export const DEV_CSP = CSP.replace(
  "script-src 'self'",
  "script-src 'self' 'unsafe-inline'",
).replace("connect-src 'self'", "connect-src 'self' ws: wss:");

export function securityHeaders(dev: boolean): Record<string, string> {
  return {
    "Content-Security-Policy": dev ? DEV_CSP : CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
  };
}
