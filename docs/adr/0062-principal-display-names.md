# 0062. Display names for principal labels

Status: Accepted
Date: 2026-10-11

## Context

Audit events, change sets and revisions identify people as labels (`user:<provider subject>`, `token:<name>`,
`local:<name>`). The UI had to derive names from the label, which shows opaque subject ids for people.

## Decisions

1. `GET /tenants/{t}/principals?actor=...` (repeat `actor`, up to 100) resolves labels to `{actor, kind, display_name}`, in
   the order given, one answer per distinct label. `GET /principals` does the same for system administrators across tenants.
   A batch lookup keeps list responses unchanged.
2. A `user:` label resolves to the `username` the identity provider returned at sign-in (`user_account.username`). Tokens,
   break-glass and system labels are named from the label (`ci-bot (API token)`, `drift detector (system)`), and an
   unresolvable label is returned as itself. Every input gets a non-empty name.
3. Inside a tenant, a user is only resolved when they appear in that tenant's own history (audit events, change set author or
   approver, revision author). Other `user:` labels get the label only, so the endpoint cannot be used to look up people of
   other tenants. No e-mail is returned.
4. If two providers give the same subject, the label is ambiguous; the lookup returns one of them. Subjects are normally unique
   per issuer, and the label has no room for the provider.
