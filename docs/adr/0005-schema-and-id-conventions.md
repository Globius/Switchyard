# 0005. Schema conventions: UUIDv7, `version`, composite tenant foreign keys, text enums

Status: Accepted
Date: 2026-10-10

## Context

The first migration sets patterns that every later table copies. The backend design asks for a
`version` column on every object, UUIDv7 ids and names unique per tenant.

## Decision

- **Ids are UUIDv7** generated in Python (`switchyard/ids.py`, RFC 9562), so they sort by creation
  time. PostgreSQL 17 has no built-in `uuidv7()`. Fixtures use UUIDv5 ids derived from symbolic
  names; `syctl seed` replaces them with UUIDv7 (`domain/loader.py: reassign_ids`).
- **Every object table has `id`, `version` (default 1, `CHECK >= 1`)**. The `tenant` table has the
  same columns.
- **Names are unique per tenant** with `UNIQUE (tenant_id, name)` on each tenant-owned table. They
  use `a-z`, `0-9`, `-` and `_` only (no dots), because names become section and file names, and the
  HAProxy Data Plane API rewrites dots in uploaded file names (`web.host.map` is stored as
  `web_host.map`).
- **Cross-object references are composite**: `FOREIGN KEY (tenant_id, x_id) REFERENCES x (tenant_id,
  id)`, backed by `UNIQUE (tenant_id, id)`. A reference cannot cross tenants (ADR 0002). Child
  tables (`vs_port`, `vs_route`, `pool_member`, ...) copy `tenant_id`, cascade on delete from their
  parent, and keep list order in a `position` column.
- **Enumerations are `text` with `CHECK` constraints**, not PostgreSQL enum types, because adding a
  value is then an ordinary migration.
- **Shape-varying details are JSONB** where the design says so (`policy_set.rules`, health monitor
  `http`, pool `persistence`, VIP pool `ranges`), validated by the domain models on every load.
  `policy_set` carries a `CHECK` that the payload matches its `kind`.
- **Constraint names are deterministic** through a naming convention (`db/base.py`), so migrations
  and autogenerate stay stable.
- **Migrations are written to be run by a privileged role** (they create the `switchyard_app` and
  `switchyard_admin` roles if absent). A test asserts that the ORM models and the migration agree
  and that downgrade then upgrade works.
- **Timestamps are `timestamptz`**; the domain rejects naive datetimes.

## Consequences

- Deleting a parent that is still referenced by a non-cascading key fails; there is no soft delete
  in M1.
- Adding a tenant-scoped table means adding `tenant_id`, the two unique constraints, the policy and
  the grants in a migration; the structural RLS test fails until all of that is present.
- The M1 migration was edited once before release (health monitor `port`). From the first merged
  release, schema changes go in new migrations.

## Alternatives considered

- **Database-generated ids** (`gen_random_uuid()`): not time-ordered, and ids would not be known to
  the in-memory snapshot before insert.
- **Single-column foreign keys with RLS only:** isolation would then rest on one mechanism.
- **Native enum types:** harder to evolve.
