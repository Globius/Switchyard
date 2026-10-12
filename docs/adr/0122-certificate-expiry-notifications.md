# 0122. Certificate expiry notifications

Status: Accepted
Date: 2026-10-12

## Context
The renew job renews certificates it manages, but a certificate that is imported, that cannot be renewed (CA down, DNS
credentials revoked) or whose renewal keeps failing just expires. The ACME work left notifications for the alerting block
(ADR 0120).

## Decision
A `cert_expiry` rule is the notification. It looks at every server certificate of the tenant (client CA bundles are
excluded), whatever its source, so a renewal that quietly fails is caught by the same rule as an import nobody renews:
the alert depends on the date, not on the renewal job. Tiers are the rule's `days` (default 30, 14, 7); crossing the next
tier sends again, expiry sends "has expired", and renewing (a later `not_after`) sends a resolved message. A new tenant
gets no rule by default (a UI can offer "tell me 30, 14 and 7 days before" with the tenant's first target).

## Consequences
A certificate already inside a tier when the rule is created sends one message at the next evaluation. Certificates
issued for a short life (days) alert on every cycle if they fall under the smallest tier; give such tenants a rule with
smaller days.

## Alternatives considered
Sending from the renew job (misses imports and silent renew failures); a fixed system-wide schedule (tenants differ).
