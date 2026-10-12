# 0146. OpenTofu provider generated from the OpenAPI document

Status: Accepted
Date: 2026-10-12

## Context

Plan item C10 (08, 09 block P12): tenants manage infrastructure as code with OpenTofu. The API already has one uniform shape for every
tenant object (`POST/GET/PATCH/DELETE /tenants/{t}/<kind>`), typed by `docs/api/openapi.json`, which a test keeps equal to the code
(ADR 0014).

## Decisions

1. **A Go provider in `provider/`**, built on `terraform-plugin-framework` (MPL-2.0; OpenTofu speaks the same plugin protocol). No
   HashiCorp BSL code is used (CLAUDE.md). It is a separate Go module, so the agent and the controller do not depend on it.
2. **Generated schema, generic behaviour.** `provider/internal/gen` reads the OpenAPI document and writes `zz_generated.go`: for each
   kind the attribute tree of its create body (nested objects and lists of objects become nested attributes; a value the generator
   cannot type becomes a string holding JSON, and at the moment there is none), the keys a PATCH accepts and the fixed discriminator.
   One piece of hand-written code does the work for all kinds, converting between Terraform values and JSON by walking the schema.
   A new field in the API is a regeneration, not new code. A Go test fails when the committed table is stale.
3. **Kinds.** Resources and data sources for network, VIP pool, SE group, virtual service, pool, health monitor and, one type per
   discriminator value, redirect and allow-list policy sets and VRRP and BGP routing profiles. Certificates are a **data source only**:
   their key material lives in OpenBao and is issued or imported through the API, never through Terraform state.
4. **Writes go through the pipeline.** Each create, update and delete is `?apply=true`, so validation, canary, revision and Git mirror
   apply exactly as for any API client. The provider polls the apply job and treats `ok: false` (rejected, reverted) as an error with
   the controller reason. Writes are serialised in the provider, because the controller serialises per SE group anyway and parallel
   writes only time out on that lock. A tenant that requires approval refuses `apply=true`; for those tenants the provider is not a
   fit until it can add to a draft change set (not built).
5. **Ids and concurrency.** The provider chooses the object id (UUIDv7, sent as `object_id`), so a dependent resource can use it at
   once. Updates and deletes send `If-Match` with the stored `version`.
6. **No surprises on plan.** Every attribute a caller may omit is optional and computed, since the controller fills defaults. Strings
   that mean the same (an IPv6 address written differently, a re-ordered JSON document) keep the spelling in the configuration.
7. Imports use the object id. Data sources look an object up by name over the list endpoint.

## Consequences

- The provider is only as good as the API schema. A field typed loosely there is loosely typed here; the generator shows it.
- Distribution (registry namespace, signing, release job) is not part of this change. `main.go` uses
  `registry.opentofu.org/switchyard/switchyard` as its address and the README describes `dev_overrides`.
- The provider was tested against an in-memory fake of the API and the framework schema validation, not against a live
  OpenTofu run. A first real `tofu plan` against the lab controller is the next check.
- The UI needs nothing from this change.
