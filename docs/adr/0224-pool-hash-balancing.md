<!--
SPDX-FileCopyrightText: 2026 The Switchyard contributors

SPDX-License-Identifier: Apache-2.0
-->

# 0224. Hash-based balancing: `balance uri`, `hdr(name)` and `url_param(name)`

Status: Accepted
Date: 2026-10-24

## Context

`Algorithm` offered only `roundrobin`, `leastconn`, `source` and `random`, and the renderer wrote
`balance <algorithm>` verbatim. A cache farm or a sharded backend cannot be expressed that way: those
want the same object, shard or tenant on the same member for as long as the member is up, which
`source` only approximates (it hashes the client address, so one client pins to one member whatever
it asks for).

## Decision

1. **Three new `Algorithm` values**: `uri`, `hdr` and `url_param` — HAProxy's names, so the enum value
   is what follows `balance`. They render `balance uri`, `balance hdr(<name>)` and
   `balance url_param <name>`; the existing algorithms keep `balance <algorithm>` unchanged.

   *Correction (2026-10-24, same day, no decision changed):* this record first said
   `balance url_param(<name>)`. HAProxy's `balance` directive is asymmetric — `hdr` takes its key in
   parentheses, `url_param` takes it as a separate word — and 3.2 answers
   `balance only supports ... 'hdr(name)' ...` for the parenthesised form. The renderer, the golden
   case and the tests now use `balance url_param <name>`, verified with `haproxy -c` 3.2.25. The
   decision (which algorithms, one `hash_key` field, no `hash-type` knob) is unchanged.
2. **One key field, `Pool.hash_key`** (a header name or a URL parameter name, 1-64 characters of
   `A-Z a-z 0-9 _ . -`). One field rather than one per algorithm because a pool uses exactly one
   algorithm, and `uri` "knows" its key (the path). `domain/validators.py:validate_hash_key` is the
   boundary check; `HashKey` in `domain/models.py` is the `Annotated` type; `render/escape.py:hash_key`
   checks the value again before it lands in a `balance` line. `%` is refused outright: HAProxy would
   read `%[...]` as a log-format expression instead of a literal name.
3. **`hash-type` stays at HAProxy's default.** The issue allows a knob only if an ADR argues for it,
   and none of the arguments hold yet: the default (`map-based`) is deterministic for a given member
   set and keeps a key on its server when an unrelated member leaves, which is exactly the property a
   cache farm needs; `consistent` redistributes a share of every key whenever the ring changes, which
   is a cache-warming decision an operator should make deliberately, not a default we pick for them.
   Adding a field later is additive (a nullable column and one renderer line); guessing wrong now
   would silently move cache keys on every pool that opted into hashing.
4. **Validation is a snapshot rule, not a model rule.** The model checks the key's shape; whether a
   key is *needed* or *unexpected* depends on the algorithm and is reported on
   `pools.<name>.hash_key` with the codes `hash_key_required` (a `hdr`/`url_param` pool without a
   key) and `hash_key_unexpected` (a key on an algorithm that ignores it, `uri` included — an unused
   field on a pool reads as effective to an operator). `hash_key_invalid` is the defensive
   counterpart for a key that reached the snapshot without the model's validator.
5. **UDP pools reject all three** with `udp_algorithm_unsupported` on `pools.<name>.algorithm`
   (plus `udp_field_not_allowed` on `hash_key`): a `udp` service is served by Keepalived/IPVS, which
   has no such scheduler (ADR 0020, ADR 0013), and `field_rules.py` lists them as forbidden so
   `GET /api/v1/schema/virtual-service` tells a form the same thing.
6. **They need HTTP.** `hash_key_needs_http` rejects `uri`/`hdr`/`url_param` on a pool a `tcp`
   service uses, like `persistence_needs_http` for header and URI persistence: HAProxy accepts these
   keywords in `mode http` only, so the alternative is a config that fails `haproxy -c`.

## Consequences

- A pool carries `hash_key` in the API, the change-set diff and the UI pool form; the golden case
  `30-hash-balancing` pins the three `balance` lines.
- Existing pools are unaffected (`hash_key` is null and none of the new algorithms is the default).
- Changing a member set rehashes part of the keyspace under the default `hash-type`; the operator
  who needs a different behaviour must wait for a `hash_type` field rather than edit the config.

## Alternatives considered

- **One field per algorithm** (`hash_header`, `hash_url_param`): two nullable columns where only one
  can ever be set, and a third validation rule to keep them exclusive.
- **Reuse `PersistenceHeader`/`PersistenceUri`**: those describe a stick table, an orthogonal feature
  (a pool may hash *and* keep persistence), and their validators answer a different question.
- **Per-pool `hash_type`** (`map-based` / `consistent`): deferred, see decision 3.
- **Accept `hdr(...)`/`url_param(...)` free-form in the algorithm value**: the enum would become a
  string, the schema could not publish the choices, and a user value would reach the config unparsed.
