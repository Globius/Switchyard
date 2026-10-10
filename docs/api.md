# HTTP API conventions

The contract is `docs/api/openapi.json` (also served at `/api/v1/openapi.json`). Regenerate it with
`UPDATE_OPENAPI=1 uv run pytest tests/unit/test_openapi.py` from `controller/`, review the diff and commit
it; CI rejects breaking changes. Background: [ADR 0014](adr/0014-typed-api-contract.md).

## Responses

- Every 2xx JSON body has a model and an example. Free-form on purpose: an operation's `body`, a job's
  `result`, an audit event's `diff`.
- Errors are `application/problem+json` (`type`, `title`, `status`, `detail`, optional `errors[]` with
  `path`, `code`, `message`). `path` addresses the input field, for example `ports.0.port`.
- Object reads return `ETag: "<version>"`. Send it as `If-Match` on `PATCH` and `DELETE`: a stale value
  answers `412`, a missing one `428`.
- Timestamps are ISO 8601 UTC.

## Objects

Reads of an object type (`GET /tenants/{t}/pools`, …) return the object's fields plus `draft_status`
(`unchanged | created | modified | deleted`) and `changed_fields`; with `?change_set=<id>` they show the
draft. Lists are `{items, next_cursor}`.

Policy sets and routing profiles are unions on `kind`:

| Collection | `kind` values | Fields |
| --- | --- | --- |
| `policy-sets` | `redirect` | `rules[]` |
| | `allow_list` | `cidrs[]`, `managed_by` |
| `routing-profiles` | `vrrp` | `vrid`, `interface`, `advert_interval_s`, `base_priority`, `priority_step`, `static_routes[]` |
| | `bgp` | `asn`, `bfd`, `peers[]`, `max_paths`, `static_routes[]` |

`kind` is required when creating and when patching (and cannot be changed).

## Pagination

`GET /tenants/{t}/change-sets` and `GET /tenants/{t}/revisions` take `limit` (1–200, default 50) and
`cursor`, and return `{items, next_cursor}`; pass `next_cursor` back to continue. Change-set items carry
`state`, `author`, `summary`, `base_revision`, `created_at` and `op_count`.

## Diff

`GET /tenants/{t}/change-sets/{cs}/diff` returns `objects`, `problems` (rules the new state breaks; they
block apply), `warnings` (`{code, path, message}`; they never block) and `se_groups` with the changed
files. Warning codes: `udp_not_served` (a UDP virtual service is stored but no engine serves it yet).
