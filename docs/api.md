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
files. There are no warning codes in use today (`udp_not_served` ended with M6, when IPVS began serving UDP).

`GET /tenants/{t}/status/virtual-services` returns `{id, name, protocol, state}` per service. `state` is
`serving`, `disabled` or `not_served` (reserved for a service no engine can render). UDP services are
`serving` since M6 (ADR 0013, ADR 0020); their real servers appear in the engine status `members`
(`backend` `pool_<name>`, `status` `UP`, `DOWN` or `DRAIN`) and, with counters, in `agent.ipvs`.

`GET /schema/virtual-service?protocol=http|tcp|udp` returns the fields the protocol rejects on the
service (`service_forbidden`), on its ports (`port_forbidden`) and on its pools (`pool_forbidden`), the
allowed pool algorithms and monitor types, and whether pool members need a port. A form can disable
fields from it; the validator reads the same table and reports each rejected field on its own
`errors[].path`.
