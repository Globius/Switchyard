# 0001. Layering: api → services → domain, with pure `domain` and `render`

Status: Accepted
Date: 2026-10-10

## Context

The backend design ("Repo layout, testing and documentation") says layers depend downward only so
render and validation logic can be tested without a database or network. Milestone M1 is the first
code, so the rule has to be made concrete before other layers start to lean on it.

## Decision

```text
api ──► services ──► domain
          │  ▲          ▲
          ▼  │          │
         db, adapters   render ──► domain
```

- `domain` holds Pydantic models and validators. It does no I/O: no database, files, network, clock
  or environment. Randomness lives only in `switchyard/ids.py`, outside the domain package.
- `render` is made of pure functions from a `Snapshot` to files. It depends on `domain` and nothing
  else (not on `db`, `services` or `adapters`). The same state always gives the same bytes.
- `db` holds SQLAlchemy models, migrations, RLS and the repository. It converts between rows and
  domain objects (`db/mapping.py`) so the ORM never leaks upward.
- `adapters` are the only code that talks to external systems. Each gets a typed interface and a
  fake for tests (none exist yet; M2 adds the first).
- `services` orchestrate: they use `db`, `domain` and `render`, and are what `api` and the CLI call.
  `services/render.py` is the M1 example: look up the tenant, read state under RLS, call the pure
  renderer.
- `api`, `pipeline` and `jobs` exist as empty packages with a docstring. There are no placeholder
  functions that pretend to work.

Enforcement is by tests and review, not tooling: `render` and `domain` are exercised without any
fixture that touches Docker, and `pytest -m "not docker"` runs them alone.

## Consequences

- Golden-file and property tests for the renderer are fast and need no containers.
- Anything that needs I/O during rendering (for example a certificate's contents) must be passed in
  through the snapshot or handled by a later pipeline stage, not fetched by the renderer.
- A change that wants `render` to import from `db` is a signal to move logic into `services`.

## Alternatives considered

- **Active-record models used everywhere.** Simpler at first, but validation and rendering would
  need a database to test, and RLS would be impossible to reason about in isolation.
- **Import-linter or similar enforcement.** Worth adding when the codebase has more than one
  contributor; deferred to avoid tooling before there is code to constrain.
