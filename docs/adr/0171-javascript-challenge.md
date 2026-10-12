# 0171. JavaScript challenge (proof of work)

Status: Accepted
Date: 2026-10-12

## Context

Plan item E10 asks for a JavaScript challenge for bot defense. Options: HAProxy Lua, a sidecar, or native HAProxy.

## Decisions

1. **Native HAProxy, no Lua and no sidecar.** HAProxy 3.2 has `sha2`, `hex`, `field`, `date` and `sub`, which is enough to check a proof of work.
   This adds no process to the SE image, no code the controller must ship besides one fixed HTML page, and nothing that can crash separately.
2. **Scheme.** `protection.challenge` (`difficulty` 3 to 5 zero hex digits, `valid_for_s`, `skip_paths`, `skip_user_agents`). A client without a valid
   cookie gets a page (`haproxy/challenge/<service>.html`, `lf-file`, so `%[date]` and `%[src]` are filled per request). Its script finds `n` such that
   `sha256("<epoch>-<n>|<client address>|<salt>")` starts with the required zeros, stores `sy_chal=<epoch>-<n>` and reloads. HAProxy recomputes the hash,
   checks the zeros and that the epoch is between 0 and `valid_for_s` seconds old, and lets the request through.
3. **Nothing secret.** The salt is a public per-service value (hash of the service and set ids), so a solved cookie is good for one address on one
   service only. A bot farm pays the work once per address and validity period; there is no key to leak, store in OpenBao or rotate.
4. **Who is challenged.** Everyone except `exempt_cidrs`, `skip_paths` (APIs, health checks) and `skip_user_agents` (search engines; the header can be
   faked). `GET` and `HEAD` get the page; other methods without a valid cookie get 403. The ACME HTTP-01 answer runs earlier and is never challenged.
5. **Limits.** It stops tools that do not run JavaScript and makes mass abuse cost CPU; it does not stop a real browser driven by a bot. Use the
   CAPTCHA gate (ADR 0172) where that matters. Behind a proxy the client address is the restored one (stage 2), like the rate limits.
6. Verified against a real HAProxy by `tests/integration/test_js_challenge.py`, which solves the challenge as the page does.
