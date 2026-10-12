# 0172. CAPTCHA gate with a pluggable provider

Status: Accepted
Date: 2026-10-12

## Context

E10 wants a CAPTCHA hook. Verifying an answer needs a call to the provider with a secret key, and a cookie that a client cannot forge needs a
secret on the engines. The JavaScript challenge (ADR 0171) needs neither; this does.

## Decisions

1. **HAProxy Lua, not a sidecar.** The engine already has Lua (3.2 official builds include it) and an HTTP client with certificate checking, so the
   gate needs no new process. The script is fixed text rendered by the controller (`render/captcha.py`); only validated constants are filled in
   (service name, provider URL and form field from a fixed table, validity, secret path). Tenants never supply script text. This stays within the rule
   against free-form snippets.
2. **Providers:** `turnstile` (Cloudflare) and `hcaptcha`, each a row of the table in `render/captcha.py` with its siteverify URL, form field and
   widget markup. Another provider is one more row and an enum value. Fields: `site_key` (public), `secret_ref` (OpenBao path, tenant namespace
   checked like other secret references), `valid_for_s`, `skip_paths`, `skip_user_agents`. `challenge` and `captcha` exclude each other.
3. **Flow.** A client without a valid `sy_cap` cookie gets a page with the widget (`GET`/`HEAD`) or 403. The page posts the token to
   `/.sy/captcha`. The Lua service sends `secret`, `response` and `remoteip` to the provider over HTTPS (`httpclient.ssl.verify required`), and on
   `"success": true` sets the cookie and redirects to the original local path (a target that is not a plain local path becomes `/`).
4. **Cookie.** `sy_cap=<epoch>.<hmac>`: HMAC-SHA256, key `sha256("sy-captcha|" + provider secret)`, over `<epoch>|<client address>|<service>`; HttpOnly,
   SameSite=Lax, Secure on TLS. It is bound to one address and one service and expires after `valid_for_s`. Rotating the provider secret ends every
   session. HMAC is built from HAProxy's `sha2` converter because Lua has no HMAC call; it was checked against Python's `hmac`.
5. **Secret delivery.** The provider secret is not in `haproxy.cfg`, the Lua file, Git or PostgreSQL. The controller reads it from OpenBao and puts it in the
   signed agent bundle as `certs/captcha-<service>.secret` (owner-only like private keys; the agent accepts the `.secret` suffix in the certs
   directory and reloads HAProxy). The script reads it once at start. A missing file makes the gate refuse everyone (fail closed), never pass.
6. **Network.** The engine needs outbound HTTPS to the provider. The config adds `resolvers sy_dns` (`parse-resolv-conf`) for the HTTP client. A
   provider outage means new clients cannot pass; clients with a valid cookie are unaffected.
7. **Tested** against a real HAProxy and Cloudflare's published always-pass Turnstile test keys (`tests/integration/test_captcha_gate.py`, skipped
   when the network is not reachable). Not tested against hCaptcha or a live production key.
