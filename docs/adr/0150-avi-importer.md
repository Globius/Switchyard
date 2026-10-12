# ADR 0150: Avi configuration importer

Status: Accepted

## Context

Switchyard replaces NSX ALB (Avi). The migration plan recreates every service by hand; the services
that matter are few, but each carries pools, health checks, certificates, redirects and SNI routing that
are tedious and error-prone to retype. The export of an Avi controller holds all of it.

## Decision

- A **pure converter** (`switchyard.importers.avi`) maps the export's JSON to Switchyard object bodies.
  It reads the objects a migration needs, follows references from the virtual services (so unused
  objects stay behind unless asked for), and records a finding for every decision: `info`, `warning`,
  `manual`, `skipped`. Nothing is dropped without a finding.
- An SNI parent and its children become **one HTTP service with host routes**, the pattern the design
  documents for the existing estate. HTTP policy rules become redirect and pool-switch routes; an
  unconditional HTTP-to-HTTPS redirect becomes `https_redirect_port`.
- **VIPs do not carry over** (design 01: the platform is net-new). The caller gives each Avi VIP a new
  address from a VIP pool or an explicit map.
- The **service layer** (`AviImportService`) resolves the SE group and the VIP pool, avoids names the
  tenant already uses, imports certificates through the certificate service (keys go to the secrets
  backend, never into a change set), and opens **one draft change set**. It never applies anything.
  `dry_run` is the default.
- Everything unsupported is reported as `manual` (WAF, DataScripts, bot and security policies, external
  monitors, rewrite and header actions, other persistence kinds), so the gaps are a worklist, not a surprise.

## Consequences

- The converter targets the object model of today; new policy and pool features (P1, P2) can be mapped
  by extending the converter, and the findings say what is not mapped yet.
- The export is large and holds secrets; the API accepts up to 64 MiB and the CLI reads a local file.
  The export is never stored or logged.
- A pool group becomes its first pool; weighted multi-pool routing is not modeled.
