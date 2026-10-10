# 0014. Typed API contract: response models, unions and cursors

Status: Accepted

## Context

Only requests had models. Responses were `object` and `Page{items: object[]}`, so a client generated
from `docs/api/openapi.json` typed every read as `unknown`, and policy sets and routing profiles were
free-form dictionaries. Tickets T2 and T4 in `docs/design/05-frontend-backend-tickets.md` ask for typed
bodies and a published `kind` union.

## Decisions

1. **Every 2xx body has a model** (`controller/switchyard/api/schemas.py`). The models forbid unknown
   fields, and FastAPI validates each response against its model, so a handler that returns a body that
   does not match fails with a 500 in tests instead of drifting silently.
2. **`*Read` = domain model + `draft_status` + `changed_fields`**, built from the domain model, so a
   new domain field reaches the API and the OpenAPI document without a second edit. `Page[T]` is generic;
   each list is `Page[XRead]`.
3. **Policy sets and routing profiles are discriminated unions on `kind`** (`redirect | allow_list`,
   `vrrp | bgp`) for create, patch and read. `kind` is required in create and patch bodies (it is how
   the union is told apart); a patch cannot change an object's kind. Field errors keep their plain path:
   the union tag Pydantic adds to the error location is stripped, so `rules.0.redirect.location` is the
   same path a form already uses.
4. **Errors are `application/problem+json` in the document too**, with one `ProblemOut` schema; FastAPI's
   own 422 shape is replaced. Object reads document the `ETag` header; PATCH and DELETE document 412
   and 428.
5. **Cursors on change sets and revisions.** Both lists now return `Page` (`items`, `next_cursor`)
   with `limit` and `cursor`. The cursor is the last change-set id (UUIDv7, so creation order) or the
   last revision number. Change-set list items gain `base_revision`, `created_at` and `op_count`.
   This is a breaking change of the list shape; no client had been released.
6. **`warnings[]` on the diff** (`{code, path, message}`, never blocks apply), next to `problems[]`.
   The renderer reports structured warnings (`RenderResult.notices`) next to its existing text
   (`warnings`); the first code is `udp_not_served`.
7. **A documented example on every 2xx JSON response** (`api/examples.py`), validated against the
   models by a unit test, so mock servers can be generated from the document.
8. **Free-form on purpose:** the partial `body` of a change operation, a job's `result` and an audit
   event's `diff`. Revision `snapshot` is the typed `Snapshot`, so stored snapshots must stay readable
   by the domain models (already required for rollback).

## Consequences

- Generated TypeScript narrows on `kind` and has no `unknown` for 2xx bodies except the three fields
  above; `tests/unit/test_api_schemas.py` fails when a new untyped field appears.
- `datetime` fields serialize with a `Z` suffix instead of `+00:00`.
- T3 (rendered config) and T5 (jobs) extend these models rather than replacing them.

## Alternatives considered

- Keep lists as arrays and put the cursor in a `Link` header: non-breaking, but different from every
  other list and awkward in a generated client.
- Separate `oneOf` request schemas without a discriminator: forms could not narrow on `kind`.
