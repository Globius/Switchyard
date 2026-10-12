# 0170. Bot and DDoS protection set

Status: Accepted
Date: 2026-10-12

## Context

Plan item E10: bot defense, DDoS profiles, login-path rate limits and API quotas per key. Rate limits per path and per key already exist
(`rate_limit`, ADR 0110). What was missing is protection below the request level (connections, slow clients) and cheap bot rules. The WAF
(ADR 0140) stays the place for signatures.

## Decisions

1. A policy-set kind `protection`, attached with `policy_set_ids`, `http` services only, one per service (`too_many_protection_sets`). A set must
   switch on at least one protection.
2. **Per-source connection limits:** `max_connections_per_source` and `max_new_connections_per_source` (per 1, 10 or 60 seconds). They run at
   connection time (`tcp-request connection track-sc<n> src ...` and `reject`), before the allow-list session rule and before TLS or HTTP parsing, so
   a flood costs the engine as little as HAProxy allows. The table is a dummy backend `pt_<service>` (`conn_cur,conn_rate`), and its counter slot
   comes after the service's rate-limit slots. Tables count towards the tenant quota `stick_table_entries` (ADR 0110).
3. **Slow clients:** `header_timeout_s` renders `timeout http-request` and `client_timeout_s` renders `timeout client` in the frontend, the answer to
   slow-header and idle-connection attacks.
4. **Bot rules:** `block_empty_user_agent`, `block_scanners` (a fixed list of well-known scanner names, `KNOWN_SCANNERS`, in the model) and
   `deny_user_agents` (up to 32 lower-case substrings of letters, digits and `._/+-`). They render as `http-request deny` (403) or `silent-drop`
   (`bot_action`) on the lower-cased `User-Agent`. They sit after the rate-limit rules and before redirects and routing.
5. `exempt_cidrs` (up to 32) are accepted before any connection rule and skipped by the bot rules, for monitors and partners.
6. **Limits of the design.** The address is the connection source, so behind Cloudflare or a PROXY-protocol load balancer the connection rules see
   that device (use the request-based `rate_limit` rules there; the client address is restored only at the request stage). A user-agent rule stops
   lazy tools, not a determined bot; JavaScript challenges, reputation feeds and CAPTCHA are not built. Volumetric DDoS is handled upstream
   (BGP, the provider), not by the engines.
7. Storage: the settings are the single element of `policy_set.rules`; the kind check gains `protection` (migration 0032).
