# 0131. Access rules and authorization

Status: Accepted
Date: 2026-10-12

## Context

Plan item E2: require a login, or a group, or an e-mail domain, per route or path. The architecture is in
[0130](0130-auth-service-architecture.md).

## Decision

An `access` policy set has `default_mode` (`login`, the closed default, or `public`) and up to 32 ordered `rules`. A rule is
`{match, mode}` with a `RouteMatch` ([0101](0101-route-match-model.md); empty matches everything) and one of:

| `mode` | Needs | Extra field |
| --- | --- | --- |
| `public` | nothing; the request never touches the proxy | none |
| `login` | any user the identity provider vouches for | none |
| `groups` | a member of at least one listed group | `groups[]` (names or ids, `A-Z a-z 0-9 . _ : / @ = -`) |
| `email_domains` | an address in one of the domains | `email_domains[]` (host names) |

The **first rule that matches decides**; a request that matches none gets `default_mode`. Matching happens on the first hop,
after rewrites, so rules see the final path.

**Decision value.** HAProxy keeps the decision in `txn.sy_ad`: 1 means public, `k + 2` means rule *k* (0-based), and
`len(rules) + 2` means the default needs a login. A value of 2 or more sends the request to the proxy. The value travels to the
second hop in the header `X-Sy-Access`, which the first hop removes from every client request before using it (decision
values must not be forgeable) and the second hop removes before the origin. The second hop does not re-match, so a rewrite or the
proxy cannot move a request to a different rule.

**Enforcement on the second hop**, in this order:

1. no `X-Sy-Access` header: 403 (someone connected to the loopback bind directly);
2. decision 2 or more without `X-Forwarded-User`: 401;
3. a `groups` rule: 403 unless the full `X-Forwarded-Groups` header matches `(^|,)(g1|g2)(,|$)`. The header is read with
   `req.fhdr` because HAProxy's `req.hdr` would split the list at its commas;
4. an `email_domains` rule: 403 unless `X-Forwarded-Email` matches `@(d1|d2)$` case-insensitively.

Group names and domains are validated and escaped (`.` becomes `[.]`); they are matched as whole items, not substrings.
oauth2-proxy is configured with `email_domains = ["*"]` so it authenticates everybody the IdP accepts and leaves authorization
to the rules, which differ per path.

## Consequences

- A user removed from a group loses access at the next session refresh (`cookie_refresh_s`, default 15 minutes), because the
  groups come from the session.
- There is no negation and no OR across rules except by listing several rules or several groups.
- 401 and 403 are plain HAProxy responses; a service can brand them with an `error_pages` set (ADR 0103).
- Auth decisions appear in the request log as the status code of the second hop (ADR 0133).

## Alternatives considered

- **Per-rule proxy instances with `--allowed-group`.** One process and one login cookie per rule; sessions would not carry
  across rules.
- **Claims other than groups and e-mail.** Add `claims[]` when a tenant asks; they need a header per claim.
