# 0003. Change operations: the model is fixed now, the code arrives in M2

Status: Accepted
Date: 2026-10-10

## Context

The backend design ("Tenancy and edits", "Change pipeline") says writes go through change sets, which
store a list of operations rather than shadow copies of tables. M1 does not build change sets or the
apply pipeline, but its schema and domain model must not make them hard to add.

## Decision

The operation model from the design is the contract for M2. M1 adds no code for it (no stubs):

```text
change_op = (object_type, object_id, action, body, expected_version)
  action ∈ { create, update, delete }
```

What M1 locks in so that model can be implemented without reshaping data:

- **Every object row has an integer `version`**, starting at 1, with `CHECK (version >= 1)`. Apply
  will fail an operation whose `expected_version` no longer matches (HTTP 412 on the API).
- **The unit of preview and render is a `Snapshot`** (`domain/snapshot.py`), an immutable value. A
  draft preview is "the latest snapshot with the operations applied in memory", then
  `validate_snapshot` and `render_haproxy`, with no database writes.
- **Rendering and validation take a snapshot, never a connection.** The same code serves live state,
  a draft and a historical revision (rollback).
- **A revision is a serialised snapshot** (`Snapshot.model_dump(mode="json")`). The models forbid
  unknown fields, so a stored revision either loads exactly or fails loudly.
- **Object names are unique per tenant and kind**, enforced by the database and by
  `validate_snapshot` (`duplicate_name`), so a draft is rejected before apply.
- **`db/repository.py` only inserts whole snapshots and loads them.** Per-object CRUD and
  version-checked updates belong to the service layer in M2, where they will be written against
  change operations rather than direct edits.

## Consequences

- M2 can add `change_set`, `change_op`, `revision` and `deployment` tables in new migrations without
  changing the M1 tables.
- Until M2, nothing in the codebase can modify an existing object. `syctl seed --example` is the
  only writer and refuses to run twice.
- Because there is no direct-edit path, M2 does not have to retire one.

## Alternatives considered

- **Direct CRUD first, change sets later.** It would create a second write path that the design
  explicitly rules out ("without a second write path").
- **Shadow tables per draft.** Heavier, and diffing becomes a table comparison instead of an
  operation list.
