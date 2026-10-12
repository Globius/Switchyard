# ADR 0200: SBOMs in CI and REUSE licensing

Status: Accepted

## Context

Switchyard had no licence ("TBD" in the README), no per-file licence information and no software bill of
materials (SBOM). Operators in regulated environments ask for both, in SPDX form.

## Decision

- **Licence: Apache-2.0** as the default, because none had been chosen. It is permissive, carries a patent grant, and
  suits an NSX ALB replacement that others will deploy. Miguel can change it: replace `LICENSE`, `LICENSES/`,
  the identifier in `REUSE.toml` and in the file headers (`reuse annotate --merge-copyrights` or a scripted rewrite).
- **Per-file information with REUSE** (fsfe). Source files (`.py`, `.go`, `.ts`, `.tsx`, `.sh`) carry
  `SPDX-FileCopyrightText` and `SPDX-License-Identifier` headers. Everything else (docs, golden files, lock
  files, images, generated files) is covered by `REUSE.toml`. Golden test inputs and expected outputs must stay
  byte-exact, so they never carry headers. CI runs `reuse lint`.
- **SBOMs with Syft**, in SPDX 2.3 JSON, free and open source (Apache-2.0). Separate documents for the controller
  (`uv.lock`), the UI (`pnpm-lock.yaml`), the Go agent, the OpenTofu provider, the SE image (the image the CI builds
  from `se-image/`) and the Helm chart. They are job artifacts on every pipeline on main and on `v*` tags, and on
  tags they are also uploaded to the Package Registry and attached to the GitLab release.

## Consequences

- Copyright holder is "The Switchyard contributors"; change it in `REUSE.toml` if a legal entity should hold it.
- Syft has no Helm cataloger: the chart SBOM lists the chart's files and the container images it references are
  not expanded. The controller image is covered by the controller SBOM (same lock file).
- New files need a header (or fall under `REUSE.toml`); `reuse lint` fails the pipeline otherwise.
