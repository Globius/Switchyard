# 0147. Country and reputation IP lists

Status: Accepted
Date: 2026-10-12

## Context

Plan item C11 (08, 09 block P12): refuse traffic by country and by IP reputation, like the managed lists of other load balancers. The
Cloudflare allow list already shows the pattern: an allow-list policy set that a job keeps current through a change set. The
policy model of block P1 (conditions on a request) is not merged, so this change stays on the existing objects.

## Decisions

1. **One object, three managers.** `AllowListPolicySet.managed_by` accepts `geo` and `reputation` besides `cloudflare`. A `geo` list
   has `countries` (ISO 3166 alpha-2, upper case, unique, at most 250); a `reputation` list has `feed`, the name of a feed the operator
   configured. Such a list may be empty until its first sync (every other list still needs a network). Migration 0026 adds the
   columns and widens the `managed_by` check.
2. **A deny list.** `VirtualService.deny_list_id` points at an allow-list policy set and refuses its networks (UDP services cannot use
   it). HTTP: `http-request deny deny_status 403 if { src -f <list> }`, placed after the client address is restored, so behind
   Cloudflare the real client is judged, and before any other rule. TCP: `tcp-request session reject if { src -f <list> }`. The list
   file is the same sorted `.cidr` file as for allow lists, matched with HAProxy's tree lookup, so tens of thousands of networks
   cost little.
3. **Sources are the operator's, never the tenant's.** A tenant names countries and a feed. The controller fetches from
   `SWITCHYARD_GEO_URL` (a template with `{cc}`, for example ipdeny zone files, or a mirror of MaxMind data) and
   `SWITCHYARD_REPUTATION_FEEDS` (JSON name to URL: Spamhaus DROP, a CrowdSec or blocklist export). Only http(s), no redirects, at
   most 16 MiB, one network or address per line, `#` and `;` comments. An answer in which more than one line in five is not a network
   (a web page, an error) is refused. The CrowdSec bouncer API and MaxMind binary databases are not read directly; export them to
   this format.
4. **A job, every six hours.** `sync_ip_lists` compares each managed list with its source and sends one change set per tenant
   (`system:iplists`) through the normal pipeline, waiting for approval where required. Safety: a failed fetch, an empty answer,
   more than 100,000 networks, or a list that would lose more than half of at least 20 networks leaves the list as it is (a truncated
   download looks like a collapse); at most one such change set is open per tenant. Lists managed by `cloudflare` stay with the
   Cloudflare job.
5. **Empty is safe in the direction that matters.** An empty deny list denies nothing. An empty list used as an enforced allow list
   rejects everyone until its first sync, which is the safe failure for an allow list.

## Consequences

- When block P1 lands, a list reference can become a condition (`source in list`) in its policy sets; `deny_list_id` then stays as the
  simple form. P1 and this change both touch `render/haproxy.py` and `models.py`: whichever merges second rebases.
- A country list is only as good as the source's data; geolocation by address is approximate.
- UI work: policy-set form for the two new managers (countries, feed), a deny-list picker on the service form, and the "managed by"
  note on lists. Listed in the MR.
