# 0140. WAF policy set

Status: Accepted
Date: 2026-10-12

## Context

Plan item C6 (08) and the `waf_policy` row of 06: a signature WAF per service, per tenant, versioned, on the open-source stack from 01 (Coraza with
the OWASP Core Rule Set through HAProxy's SPOE).

## Decisions

1. The WAF is a policy-set kind, `waf`, so it gets everything policy sets already have: tenant ownership, versions, change sets, audit, the API
   under `/policy-sets`, RLS, and attachment through `policy_set_ids`. There is no separate table beyond the new kind (migration 0032 widens the
   kind check; the settings are the single element of `policy_set.rules`).
2. Fields: `mode` (`detect` by default, or `block`), `crs_version`, `paranoia_level` 1 to 4, `inbound_threshold` and `outbound_threshold`,
   `disabled_rule_ids` (up to 64), `excluded_paths` (up to 16, `path_beg`), `inspect_body`, `fail_mode` and `timeout_ms`. All are numbers, members
   of fixed sets, or validated paths. There are no custom rules and no free-form directives in this phase, which keeps CLAUDE.md's rule about
   snippets intact. Custom rules, if ever needed, will be a separate kind with its own ADR.
3. A service carries at most one WAF policy (`too_many_waf_policies`). It applies to `http` services only (tcp and udp services cannot have policy
   sets).
4. The same policy can protect several services; each service is its own Coraza application so logs and anomaly scores stay separate.
