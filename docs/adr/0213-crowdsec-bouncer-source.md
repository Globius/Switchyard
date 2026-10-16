# 0213. CrowdSec as a reputation source

Status: Accepted
Date: 2026-10-16

## Context

Plan 01 maps IP reputation and bot protection to "CrowdSec + HAProxy bouncer" (parity item C11). ADR 0147 added managed reputation
lists from plain-text feeds and said the CrowdSec bouncer API was not read directly. Operators who run CrowdSec want its decisions
(local scenarios plus community blocklists) enforced on every service without a second mechanism on the engines.

## Decisions

1. **The controller is the bouncer.** The controller reads the CrowdSec local API decision stream
   (`GET /v1/decisions/stream?startup=true`, header `X-Api-Key`) and exposes it as the reputation feed `crowdsec`. A tenant uses it
   like any feed (`managed_by: reputation`, `feed: crowdsec`) and attaches it with `deny_list_id`. Nothing runs on the engines
   beyond what ADR 0147 already renders: the engines do not talk to CrowdSec and keep serving if it, or the controller, is down.
2. **Operator configuration, never tenant.** `SWITCHYARD_CROWDSEC_URL` (http(s)), `SWITCHYARD_CROWDSEC_API_KEY` (a bouncer key,
   like `SWITCHYARD_CLOUDFLARE_TOKEN` an environment value, not stored in PostgreSQL) and optionally `SWITCHYARD_CROWDSEC_ORIGINS`
   (comma separated, for example `crowdsec,CAPI`). A URL without a key is refused at start; the name `crowdsec` cannot be reused
   in `SWITCHYARD_REPUTATION_FEEDS`.
3. **What counts.** Only `ban` decisions with scope `Ip` or `Range`. Captcha and throttle decisions and country, AS and user scopes
   are skipped, since HAProxy matches on source address only here. The same limits as every list apply (100,000 networks, no
   redirects, 16 MiB, shrink and empty guards).
4. **Same job, same safety.** `sync_ip_lists` (six hours, change set from `system:iplists`) is reused, so canary, approval and the
   guards against a failed or collapsed fetch apply unchanged. The stream is read with `startup=true` on each run rather than
   by keeping state, so a missed delta cannot leave the list wrong.

## Consequences

- Freshness is the sync interval (six hours), not seconds: this is list-based blocking, not live remediation. Faster remediation
  needs a shorter interval or a stick-table/SPOA bouncer, left for a later ADR.
- No new migration: the feed name is already a free text field on reputation lists.
- Captcha and bot challenges (parity item for bot management) are not covered.
