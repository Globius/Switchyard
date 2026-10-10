# 0015. Rendered configuration per object

Status: Accepted
Date: 2026-10-10

## Context

The only rendered output the API exposed was the change-set diff, whose `files` entries had no
documented shape. Every object page in the UI needs a *Rendered config* tab, and the Monaco diff view
needs both sides of a file, not only a unified diff.

## Decision

- **Endpoint.** `GET /tenants/{t}/{virtual-services|pools|health-monitors|certificates|policy-sets|se-groups}/{id}/rendered`,
  with optional `?change_set=` to read the draft. It returns `{items: [{se_group_id, file, language,
  content, fingerprint, range}]}`. Networks, VIP pools and routing profiles render no HAProxy text and
  have no such route (routing profiles render per engine into `keepalived.conf`/`frr.conf` and need
  secrets, which this read path does not touch).
- **One renderer.** `render/fragments.py` renders the SE group with `render_haproxy` and selects text
  from the result; nothing is re-rendered or re-derived, so fragments are exactly what engines run and
  inherit ADR 0004's determinism. It is pure and does no I/O.
- **Fragments are slices.** `content` is always the whole file and `range` the 1-based inclusive line
  span of the object's part (`null` when the object owns the whole file), so the editor shows context
  and highlights the span. `fingerprint` is the SE group's render fingerprint, the same value drift
  detection compares.
- **An object can appear in several groups** (a pool or certificate used by services in two groups):
  `items` has one entry per fragment, ordered by group name. An object that renders nothing (unused
  pool, disabled or UDP service) gives an empty list, not an error. A draft that cannot render gives
  422; an unknown or draft-deleted object gives 404.
- **Diff files are typed.** `GET …/change-sets/{cs}/diff` now declares `se_groups[].files[]` as
  `{path, status, diff, before, after}`. `before` is `null` for added files and `after` for removed
  ones. The existing fields are unchanged, so this is additive.

## Consequences

- Draft and live fragments can be compared by the UI, and they differ exactly where the diff says
  (covered by a unit test that renders both and diffs).
- Fragment selection is by block header (`frontend vs_<name>`, `backend pool_<name>`), so a template
  change that renames those headers must update `fragments.py`; golden fragment files in
  `controller/tests/golden_fragments/` catch it.
