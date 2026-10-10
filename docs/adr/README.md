# Architecture decision records

One file per decision that is expensive to reverse. Use the next free number, keep the filename
`NNNN-short-title.md`, and never rewrite an accepted record: add a new one that supersedes it and
change the old record's status line.

Template:

```markdown
# NNNN. Title

Status: Proposed | Accepted | Superseded by NNNN
Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Alternatives considered
```

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-layering.md) | Layering: api → services → domain, with pure `domain` and `render` | Accepted |
| [0002](0002-tenancy-rls.md) | Tenancy is enforced in the service layer and again by PostgreSQL RLS | Accepted |
| [0003](0003-change-op-model.md) | Change operations: the model is fixed now, the code arrives in M2 | Accepted |
| [0004](0004-renderer-determinism-and-safety.md) | The renderer is deterministic, validated by real binaries, and rejects unsafe values | Accepted |
| [0005](0005-schema-and-id-conventions.md) | Schema conventions: UUIDv7, `version`, composite tenant foreign keys, text enums | Accepted |
| [0006](0006-http-routing-semantics.md) | HTTP routing order, allow lists and drain semantics | Accepted |
| [0008](0008-agent-routing-and-status.md) | The SE agent, routing profiles, and status/drift | Accepted |
