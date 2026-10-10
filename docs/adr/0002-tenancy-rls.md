# 0002. Tenancy is enforced in the service layer and again by PostgreSQL RLS

Status: Accepted
Date: 2026-10-10

## Context

Every object belongs to a tenant. A missing `WHERE tenant_id = ...` in application code must not leak
another tenant's data. The backend design requires enforcement twice: service-layer checks and
row-level security (RLS). CLAUDE.md adds that RLS is never disabled in application code and that
admin access uses a separate database role.

## Decision

1. **Scope per transaction.** Tenant-scoped work runs in a transaction that first executes
   `set_config('app.tenant_ids', '<uuid>,<uuid>', true)` (`db/tenancy.py: tenant_session`). The
   setting is transaction-local.
2. **One policy per table.** Every table with a `tenant_id` column has `ENABLE` and `FORCE ROW LEVEL
   SECURITY` and a `tenant_isolation` policy with `USING` and `WITH CHECK` of
   `tenant_id = ANY (app_tenant_ids())`. The `tenant` table uses `id`. `FORCE` makes the table
   owner subject to the policy too.
3. **Fail closed.** `app_tenant_ids()` returns `NULL` when the setting is missing or empty, so no row
   matches. A malformed value raises an error instead of returning rows.
4. **Two roles.**
   - `switchyard_app` (`NOLOGIN`, `NOBYPASSRLS`): used by the API and tenant-scoped workers. It can
     read and update tenants but cannot insert or delete them.
   - `switchyard_admin` (`NOLOGIN`, `BYPASSRLS`): used for system administration, seeding and
     cross-tenant jobs. Code switches by connecting as this role (`make_engine(url, role=...)`),
     never by turning RLS off.
   Deployments connect with login roles that are members of exactly the role each process needs; the
   API process must not be a member of the admin role.
5. **Composite foreign keys.** Cross-object references are `(tenant_id, x_id)` keys into
   `(tenant_id, id)` unique constraints, so a row cannot reference another tenant's object even for
   a session that bypasses RLS (see ADR 0005).
6. **Structural test.** A test lists every table with a `tenant_id` column and asserts it has RLS
   enabled, forced and at least one policy, so a future table cannot ship unprotected.

## Consequences

- Tenant isolation is verified against real PostgreSQL (`tests/integration/test_rls.py`): tenant A
  cannot read, update, delete, insert or move rows of tenant B, with no scope sees nothing, and a
  garbage scope fails.
- Looking up an object by a name that spans tenants (for example `syctl render --tenant`) needs the
  admin role for the lookup and the app role, scoped, for the data. `services/render.py` does this.
- Policies compare against a `uuid[]`; there is no per-row subquery, so the cost is a constant-time
  array membership test per row.
- A tenant-scoped session can only be as trustworthy as the code that sets the scope. The service
  layer still checks permissions; RLS is the backstop, not a replacement.

## Alternatives considered

- **Schema or database per tenant.** Strong isolation but a migration and connection-pool cost that
  grows with tenants; the design expects many small tenants.
- **Policies keyed on `current_user`.** Would need a database role per tenant or user, which fights
  connection pooling.
- **Application-only checks.** Rejected by the design: one missed filter is a data leak.
