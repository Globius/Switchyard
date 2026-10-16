# Snyk scanning on GitHub

The GitHub mirror runs [Snyk](https://snyk.io) on every push to `main`, every pull request and once a week
(Mondays, 04:17 UTC), from `.github/workflows/snyk.yml`. GitLab remains the canonical host and keeps its own
scanners; this is an addition on the public front door, not a replacement.

| Scan | What it covers | Code scanning category |
| --- | --- | --- |
| Snyk Open Source | Python (`controller/uv.lock`), npm/pnpm (`ui/`), Go (`agent/`, `posture-agent/`) | `deps` |
| Snyk Code | Static analysis of the source | `code` |
| Snyk IaC | `deploy/` (Compose, Helm), `se-image/` and `provider/` (OpenTofu) | `iac` |
| Snyk Container | The controller and proxy images, built from `deploy/controller/Dockerfile` and `deploy/proxy/Dockerfile` | `container-controller`, `container-proxy` |

Each scan writes SARIF and uploads it to **Security → Code scanning**, so findings show on the pull request and in
the repository's security tab.

## Behaviour

- **Findings do not fail the run.** They are reported in code scanning; triage them there. Only a scan that
  could not run (a Snyk error) turns the job red.
- **No `SNYK_TOKEN`, no scan, no red.** Forks and pull requests from forks have no access to repository secrets, so
  every step is skipped with a notice. The same happens until the secret is added.
- Actions are pinned to commit SHAs, as in the other workflows.

## One-time setup (maintainers)

1. In Snyk, create a service account token (Organization settings → Service accounts, role *Collaborator*)
   or use a personal API token from Account settings.
2. In GitHub, under *Settings → Secrets and variables → Actions*, add the repository secret **`SNYK_TOKEN`**. If the
   token's default organization is not the one to use, also add the repository variable **`SNYK_ORG`** (the
   organization ID or slug).
3. In Snyk, *Add project → GitHub → Globius/Switchyard*. This is what makes the README badge show vulnerability
   counts; before the import it reads "monitored".
4. Run the workflow once from *Actions → snyk → Run workflow* and check *Security → Code scanning*.

Snyk Code must be enabled for the organization (Settings → Snyk Code); without it the `code` job reports an
error. The README badge is the repository-scoped one:

```markdown
[![Snyk security](https://snyk.io/test/github/Globius/Switchyard/badge.svg)](https://snyk.io/test/github/Globius/Switchyard)
```
