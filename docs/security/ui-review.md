# Security review: `ui/`

Date: 2026-10-11. Scope: `ui/` at the tip of `claude/ui-coverage-fix` (!76), read-only. Method: code reading plus
`grep` sweeps, and `pnpm audit` for dependencies. Nothing was run against a live deployment, so the serving
findings (headers, CSP) describe what the repository contains, not what a given environment sends.

## Summary

| # | Area | Finding | Severity |
| --- | --- | --- | --- |
| S1 | Serving | No CSP or other security headers are set anywhere in the repo; nothing in `deploy/` or the controller serves or fronts the UI | Medium |
| S2 | Login return path | `safeNext` accepts `/\host`; the router navigation is not exploitable today, but the check is weaker than it looks | Low |
| S3 | Secrets in memory | A shown-once token stays in the TanStack Query mutation cache after the dialog closes | Low |
| S4 | Tenant switch | The shell does not remount on a tenant change; no cross-tenant data leak found, but local UI state carries over | Low |
| S5 | XSS | One `dangerouslySetInnerHTML` (QR code SVG); safe as written, fragile if the source changes | Info |
| S6 | Dependencies | `pnpm audit`: no known vulnerabilities | None |
| S7 | Token and CSRF model | Sound: HttpOnly, SameSite=Strict cookie, CSRF token held in memory only | None |

No finding is exploitable as the code stands. S1 is the one that needs a decision before a production rollout.

## Findings

### S1. No CSP or security headers (Medium)

- `ui/index.html` has no `<meta http-equiv="Content-Security-Policy">`. `ui/vite.config.ts` sets no headers.
- The controller (`controller/switchyard/api/app.py`) does not serve the built UI (no `StaticFiles` mount), and
  `deploy/prod` and `deploy/controller` contain no web server config. A grep for `Content-Security-Policy`,
  `X-Frame-Options`, `frame-ancestors`, `nosniff` and `Strict-Transport-Security` across `deploy/`, `docs/` and the
  API finds nothing. So whatever serves the UI today sends no policy, and the repo gives the operator no template.
- Why it matters here: the UI is an admin console with a long-lived HttpOnly cookie. Without CSP, any future XSS
  runs with full session power, and the console can be framed (clickjacking on Approve / Apply buttons).
- Constraints a policy must allow, from the code:
  - Monaco runs workers through Vite's `?worker` import (`src/lib/monaco.ts`), so `worker-src 'self' blob:`.
  - Monaco and Tailwind inject styles, so `style-src 'self' 'unsafe-inline'` (Monaco does not support nonces
    reliably).
  - The only network targets are same-origin: `/api/...` fetches and an `EventSource` to
    `/api/v1/tenants/{t}/status/stream`. No CDN, font host or analytics is referenced (Geist is bundled through
    `@fontsource-variable`).
  - Suggested starting policy:
    `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:;
    img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.
    `form-action 'self'` fits the login flow: the OIDC/SAML login is a same-origin link to
    `/api/v1/auth/login/{provider}`, and the redirect to the provider is a server-side 302 (the CSP form-action
    directive can interfere with redirects after a form post in some browsers, so test the SAML POST binding).
- Also set: `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Strict-Transport-Security`, and
  `Cache-Control: no-store` on `index.html`.
- Fix suggestion: ship an example reverse-proxy snippet in `deploy/prod/` (and the dev server's equivalent in
  `vite.config.ts` `server.headers` so CSP breakage shows up in development), and add a runbook line.

### S2. `safeNext` is weaker than it looks (Low)

`src/features/login/Login.tsx`:

```ts
return next && next.startsWith("/") && !next.startsWith("//") ? next : undefined;
```

- `?next=/\evil.example` passes. Browsers treat `/\` like `//` in URL resolution.
- It is not exploitable today: the value goes to the router's `navigate({ to })`, which uses `history.pushState`;
  a cross-origin URL there throws a `SecurityError` instead of leaving the site. It is also echoed in the
  "Sign in to return to {next}" copy as React text (escaped).
- It becomes an open redirect if anyone later uses `window.location.assign(next)` or an `<a href={next}>`.
- Fix suggestion: also reject `\`, control characters and anything that does not parse as a same-origin path:
  `const u = new URL(next, location.origin); return u.origin === location.origin ? u.pathname + u.search + u.hash : undefined`,
  and keep the leading-`/` check. Add a unit test with `/\x`, `/%5Cx`, `//x`, `/\t/x` and `javascript:`.
- The producer side is safe: `SessionExpiredModal` sets `next` from `window.location.pathname + search`, which is
  always a local path.

### S3. Shown-once secrets remain in the mutation cache (Low)

`Access.tsx` (API token create), `SystemTokens.tsx`, `SeGroups.tsx` (enrollment token) and `IdentityProviders.tsx`
(break-glass password and TOTP secret) take the secret from a `useMutation` result and copy it into component state.
Closing the dialog clears the state (`setShown(null)`), which is right, but the mutation observer's `data` still
holds the secret, and the `MutationCache` keeps a finished mutation for `gcTime` (5 minutes by default).

- Exposure: in-memory only, readable through React Query devtools or a script already running in the page. It is
  not persisted (no `localStorage`/`sessionStorage` use for it; the only storage use in `src` is the theme).
- Fix suggestion: call `create.reset()` when the dialog closes, or give those mutations `gcTime: 0`. The shared
  `ShowOnceSecret` component could take an `onClear` callback so each caller cannot forget.
- The secret is also in the DOM while the dialog is open (`<code>` text), as designed. The QR code is drawn as
  inline SVG, so no image request carries the `otpauth://` URI. `navigator.clipboard.writeText` leaves the value on
  the clipboard until the user overwrites it; there is no clear-after timeout. Accepted by design (Copy is the
  point), worth a line in the user docs.

### S4. Tenant switch (Low)

- Query keys carry the tenant (`["t", tenant, ...]`, `["principals", tenant, ...]`, SSE patches use the same
  base), so cached data from tenant A is never shown under tenant B. The switcher navigates to `/t/{name}` and the
  SSE hook reconnects per tenant. `["tenants"]`, `["permissions"]`, `["me"]` are caller-wide, not tenant-scoped,
  which is correct.
- `Shell.tsx` renders `<Outlet />` without `key={tenant}`. On `/t/a/pools` to `/t/b/pools` React keeps the page
  component mounted, so local state survives: an open sheet, a half-typed form, a filter, a selected id. Forms are
  keyed by object id and version in the places checked (Policies, Routing profiles), so values reset when the
  data changes, but unsaved text in a form that is not keyed could be submitted against tenant B.
- Fix suggestion: `<Outlet key={tenant} />` (or key the shell content). Also call
  `queryClient.removeQueries({ queryKey: ["t", previous] })` on switch if memory growth matters.
- Not a leak, but related: on logout `qc.clear()` runs and the CSRF token is nulled (`useLogout`), which is correct.

### S5. XSS review (Info)

- Only one `dangerouslySetInnerHTML`: `ShowOnceSecret.tsx` `Qr`, which injects SVG returned by the `qrcode` library
  for the `otpauth://` URI. The library emits fixed markup (`<svg>`, `<path d>` built from module coordinates);
  the URI is encoded into the matrix, never into markup, so a hostile URI cannot add elements. If the source ever
  changes to a server-supplied SVG, sanitize it or switch to a `<canvas>` or `<img src="data:...">`.
- No `innerHTML`, `eval`, `new Function`, `document.write`, or markdown renderer in `src` (no `marked`,
  `remark`, `react-markdown` in `package.json`).
- Monaco shows rendered config and diffs as editor text, not HTML; content is passed as the `value` of a read-only editor (`ConfigEditor.tsx`) and tokenised by
  the Monarch grammars in `lib/haproxy-language.ts`. Config text is user-influenced (names, hosts) but renders as
  text. Monaco's own hover/link providers could turn URLs in config into clickable links; link opening in Monaco
  is user-initiated and goes through the browser, so no action needed beyond S1's CSP.
- Everything else is React text interpolation, which escapes. Error `detail` strings from the API are rendered as
  text.

### S6. Dependencies (None)

`pnpm audit` (dev and prod) reported no known vulnerabilities at the time of the review. Re-run in CI: add
`pnpm audit --prod --audit-level=high` to `make ui-check` or a scheduled job so new advisories show up without a
manual review.

### S7. Session, token storage and CSRF (None, with notes)

- Auth is an HttpOnly, `SameSite=Strict` session cookie (`controller/switchyard/api/app.py`,
  `set_session_cookie`), `Secure` when the public URL is HTTPS. The SPA never sees a token: no `localStorage` or
  `document.cookie` use for credentials.
- The CSRF token comes from `GET /me`, is kept in a module variable (`src/api/client.ts`), and is sent as
  `X-CSRF-Token` on every non-safe method. The server enforces it for cookie sessions and exempts bearer tokens
  (`app.py` auth dependency), which matches the design. `SameSite=Strict` plus the header is two independent layers.
- Safe-method requests (the audit export links, the SSE stream) are GETs with no state change, so they need no
  CSRF token. The export links are plain anchors; they carry the cookie, not a token, in the URL.
- Session expiry: a 401 with `code: session_expired` calls the registered handler, which opens the re-login modal
  over the page (`SessionExpiredModal`), so unsaved form input is kept; "Leave this page" goes to `/login` with a
  same-origin `next`. The modal's "I have signed in" re-checks `/me`. Nothing stores form data outside memory.
- `SameSite=Strict` note for operators: the OIDC/SAML callback is a cross-site top-level navigation. The cookie is
  set on that response and sent on the next same-site request, which works; a deep link opened from another site
  lands on the login page first if the cookie was not yet sent. That is expected, not a bug.

## Not covered

- The controller's own response headers and CORS policy (backend review).
- Anything served by the reverse proxy in front of the UI in a real environment.
- Behaviour of `monaco-editor` internals beyond how the UI configures it.

## Suggested follow-ups, in order

1. S1: add the policy and header set to a `deploy/prod` proxy example, plus the dev server, and test Monaco and the
   SAML flow under it.
2. S3: reset the secret-bearing mutations on close (one line each).
3. S4: key the shell outlet by tenant.
4. S2: harden `safeNext` and add a test.
5. S6: add `pnpm audit` to CI.

## Status after the fixes

| Finding | Status |
| --- | --- |
| S1 CSP and headers | Dev and preview servers send the suggested policy and `nosniff` / `Referrer-Policy` headers (`ui/src/lib/security.ts`); a Playwright check confirms no violations and that Monaco still loads. The production proxy (nginx in `deploy/prod/` with the CSP, HSTS and the other headers) is owned by operations in !79 |
| S2 `safeNext` | Fixed: rejects `/\`, `//`, encoded slashes, control characters and anything that is not same-origin; tests cover `/\x`, `//x`, `javascript:`, `/%5Cx`, tabs |
| S3 secrets in the mutation cache | Fixed: token and break-glass mutations use `gcTime: 0` and are reset when the dialog closes or the value has moved to component state; a test checks the cache |
| S4 tenant switch | Fixed: the shell outlet is keyed by tenant, so page state resets on a switch |
| S5 QR `dangerouslySetInnerHTML` | No change (safe as written) |
| S6 dependencies | `pnpm audit --audit-level high` is part of `make ui-check` |
