# SBOMs and licensing

Switchyard is licensed under **Apache-2.0** (`LICENSE`, and `LICENSES/Apache-2.0.txt`). The choice and the way
it is tracked are in ADR 0200.

## Licence information in the repository

The repository follows the [REUSE](https://reuse.software) specification.

- Source files (`.py`, `.go`, `.ts`, `.tsx`, `.sh`) start with two lines:

  ```text
  SPDX-FileCopyrightText: 2026 The Switchyard contributors
  SPDX-License-Identifier: Apache-2.0
  ```

- Everything else (documentation, golden test files, lock files, images, generated files) is covered by
  `REUSE.toml` at the repository root. Golden files must stay byte-exact, so they never carry headers.
- New files need a header, or must fall under `REUSE.toml`. CI fails the `reuse` job otherwise. Run it locally:

  ```bash
  docker run --rm -v "$PWD:/data" fsfe/reuse:5 lint
  ```

- Add a header to a new file:

  ```bash
  docker run --rm -v "$PWD:/data" fsfe/reuse:5 annotate \
    --copyright "The Switchyard contributors" --year 2026 --license Apache-2.0 --fallback-dot-license path/to/file
  ```

## Software bills of materials (SPDX)

[Syft](https://github.com/anchore/syft) (Apache-2.0) writes one SPDX 2.3 JSON document per component.

| File | Covers | Source |
| --- | --- | --- |
| `switchyard-controller.spdx.json` | controller and `syctl` (also the controller container image) | `controller/uv.lock` |
| `switchyard-ui.spdx.json` | web UI | `ui/pnpm-lock.yaml` |
| `sy-agent.spdx.json` | the SE agent | `agent/go.mod`, `go.sum` |
| `switchyard-provider.spdx.json` | OpenTofu provider | `provider/go.mod`, `go.sum` |
| `switchyard-se-image.spdx.json` | the SE image content: Debian packages and the HAProxy, FRR and Keepalived binaries | the image built from `se-image/` |
| `switchyard-helm-chart.spdx.json` | the Helm chart files | `deploy/helm` |

Where to get them:

- **Every pipeline on main and on `v*` tags:** the `sbom-*` jobs keep the files as job artifacts (90 days) in `sbom/`.
- **A release (`v*` tag):** the `release` job uploads them to the project's Package Registry
  (`switchyard-sbom/<tag>`) and links them on the GitLab release page.
- **By hand:** run a pipeline with the variable `SBOM=1` on any branch.

Limits: Syft has no Helm cataloger, so the chart document lists the chart's files only and does not expand the
images it references. The SE image document is made from the CI test image, which carries the same binaries as the
template but is not the template itself.

## Dependencies under other licences

Everything Switchyard ships is Apache-2.0 except unmodified third-party libraries under their own licences, which appear in the SBOM.
Deliberate exceptions are recorded here:

| Dependency | Licence | Used for | Decision |
| --- | --- | --- | --- |
| asyncssh | EPL-2.0 (or GPL-2.0-or-later) | SFTP backup targets | [ADR 0205](adr/0205-upgrades-versions-bundles-backups.md); unmodified, used under EPL-2.0 |
| crowdsec-spoa-bouncer (binary) | MIT (per its repository and CrowdSec's documentation) | CrowdSec sidecar slot in the SE image | [ADR 0215](adr/0215-crowdsec-engine-bouncer-and-signals.md); unmodified release, pinned by SHA-256 in `se-image/versions.env`; its dependencies are in the binary and appear in the SE image SBOM |
| CrowdSec Security Engine and Hub content (`deploy/crowdsec`) | MIT (engine, remediation components and hub repository per CrowdSec) | The optional engine the operator runs; not part of the SE image | ADR 0215; pulled as the official container image, not redistributed by this project. CrowdSec's Terms, EULA and community blocklist terms were not read by this project: see the CrowdSec runbook |
