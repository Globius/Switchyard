# Audit log

**What.** Every change, sign-in, approval, apply, rollback and runtime action writes an audit event: who, what, which object,
the request id and a diff. Tenants see their own events; system administrators see all.

**How to use.** `GET .../audit-events` with `actor`, `action`, `object`, `since`, `until` and `q` filters;
`GET .../audit-events/export?format=csv` (or `jsonl`) streams everything that matches. `GET /audit-actions` lists the action
names; `GET .../principals?actor=...` turns actor labels into display names.

**Limits.** An export has no row cap, so narrow it with `since` and `until`; each export is itself an audit event. Display
names resolve only for people who appear in that tenant's own history. Secrets never appear in a diff.

**Decisions:** [0060](../adr/0060-audit-export.md), [0062](../adr/0062-principal-display-names.md).
