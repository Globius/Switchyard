# 0198. Object labels and the `?label=` filter

Status: Accepted
Date: 2026-10-12

## Context
Design 04 and ticket A7 dropped `?label=` because objects had no labels, and the virtual services list
carried a disabled "Label" button (planned-vs-built audit, 2026-10-12). Tenants with a few hundred
services need a way to group them (environment, team, owner) that the controller does not have to
understand.

## Decision
- **Where.** `labels` is a map of strings on virtual services and pools, the two lists that have a
  filter bar. Other kinds (certificates, policy sets, networks, ...) keep no labels; adding them later
  is a column and a model field each, with no change to the filter.
- **Shape.** A key is 1 to 63 characters of `a-z`, `0-9`, `-`, `_` and `.`, starting and ending with
  a letter or digit. A value follows the same rule but may be empty. At most 20 labels per object.
  This is the Kubernetes label rule without upper case and without prefixes, so values copied from
  there stay valid.
- **Storage.** A `jsonb` column `labels` (not null, default `{}`, checked to be an object) on
  `virtual_service` and `pool` (migration 0039). No index: lists are filtered after the tenant's
  objects are loaded, like every other list filter.
- **Writes.** Labels are ordinary fields. They go through change sets, revisions, diffs and the audit
  log. An update replaces the whole map (the update op is a top-level merge, ADR 0007), so removing a
  label means sending the map without it.
- **Never rendered.** Labels do not reach any configuration file, so a label-only change renders to
  the same bytes and is a no-op on the engines.
- **Filter.** `?label=` is repeatable and all selectors must match: `key` (has the label), `!key`
  (does not), `key=value` and `key!=value` (absent or different). A malformed selector is a 422.
  Lists of kinds without labels do not accept the parameter.
- **UI.** The disabled "Label" button becomes a text box (comma-separated selectors, kept in the URL);
  list rows show label chips, and a chip click adds its selector. Labels are edited with a chip input
  in the virtual service settings and the pool sheet.

## Consequences
The OpenAPI document, the typed UI client and the OpenTofu provider gain a `labels` map on two
kinds. Importers (Avi, Kubernetes) set no labels. Label values are not secrets and are visible to
every role that can read the object.

## Alternatives considered
A `label` table with one row per pair: queryable in SQL, but every list would join it, and the lists
already filter in Python. Free-form tags without a value: simpler, but `env=prod` is what people
type.
