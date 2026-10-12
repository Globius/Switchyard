# ADR 0152: OWASP CRS rule catalogue for the WAF screen

Status: Accepted

## Context

A WAF policy lists the rule ids it switches off (`disabled_rule_ids`). Users find the ids in the audit
log and copy numbers. The design asks for a rule browser.

## Decision

- `GET /waf/crs-rules` (any signed-in user) returns the rule **families** of CRS 4.8.0 (id prefix,
  name, what they protect against) and the rules tenants most often disable (id, short name, paranoia
  level).
- The catalogue is **not complete** (`complete: false`). The policy still accepts any id from 1 to
  999999; the catalogue only helps to choose. It lives in `switchyard.domain.crs` and changes with the
  pinned CRS release.

## Consequences

No generated list to keep in step with the SE image. A rule missing from the catalogue is entered as
a number, as before.
