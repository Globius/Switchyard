# Contributing

Thank you for helping. You can open an issue, send a pull request, or improve the docs. This page says how, and what happens to a pull request
after you open it. The design is in [docs/design](design/01-replacement-plan.md); decisions are in the [ADRs](adr/README.md).

## Before you start

- **Small fix, typo, clear bug:** go ahead and open a pull request.
- **Anything bigger** (a feature, a change of behaviour, a new dependency): open an issue first and say what you want to do. We may already have
  a decision about it (the [why index](why.md) lists them), and it saves you work.
- **Security problems:** do not open a public issue. Use the repository's *Report a vulnerability* button on GitHub (Security tab; `SECURITY.md` in the repository root says what to include).

## Set up

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm, Go (the version in `agent/go.mod`), and Docker (the tests that touch
PostgreSQL, HAProxy and Keepalived run in containers, so they behave the same on Linux, macOS, Windows and CI).

```sh
cd controller && uv sync                         # the controller and syctl
cd ui && pnpm install --frozen-lockfile          # the web UI
```

## What a good change has

Every change ships with its tests, its docs, and passes the same gates CI runs:

| Area | Run |
| --- | --- |
| Controller | `cd controller && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -m "not docker"` |
| With containers | `uv run pytest` (needs Docker; coverage is checked at 85% for `domain`, `render` and `pipeline`) |
| UI | `cd ui && pnpm lint && pnpm format:check && pnpm typecheck && pnpm test && pnpm build && pnpm gen:api:check` |
| Agent (Go) | `cd agent && gofmt -l . && go vet ./... && go test ./...` |
| Docs | `uvx --with-requirements docs/requirements.txt mkdocs build --strict` |

- **Tests:** renderers have golden-file tests; database code is tested against a real PostgreSQL, including row-level-security isolation tests.
- **Docs:** public functions have docstrings (Google style); behaviour a user sees is documented in `docs/`; a new decision gets an ADR in `docs/adr/`.
- **Security rules** (the full list is in `CLAUDE.md`): tenancy is enforced twice, in services and in PostgreSQL RLS; no secrets in the database, logs or
  fixtures (store a `secret_ref`); every user value that reaches a config file passes a strict validator; the engine agent never opens a remote shell.
- **No** Redis, Consul, Vault or Terraform (licensing and design reasons are in the ADRs), and no change that makes the data plane depend on the controller.
- Small commits with clear messages; one change per pull request where you can.

## Sign your commits (DCO)

Every commit needs a `Signed-off-by` line, which says you wrote the change or have the right to submit it under the project's
[Apache-2.0 licence](https://developercertificate.org). Add it with:

```sh
git commit -s -m "Fix the thing"
```

A check on your pull request lists any commit that is missing it. To fix the last commit: `git commit --amend --signoff --no-edit`; for several:
`git rebase --signoff <base>` then `git push --force-with-lease`. There is no CLA to sign.

## What happens to your pull request

1. You open a pull request on GitHub. The checks (lint, types, tests, DCO, docs) run on GitHub; fix what they report.
2. A maintainer reviews it on GitHub and asks for changes, or approves and adds the label `ready-for-gitlab`.
3. That label forwards your commits, unchanged, to the project's GitLab, where code is merged (and where releases are built). You will see a comment
   with the merge request link, and a bot-opened merge request there titled `[GitHub #123] ...`. You do not need a GitLab account; keep working on
   your GitHub branch. Each new revision needs another maintainer review and label before it reaches GitLab; the bridge consumes the label after review.
4. When the merge request is merged, `main` is mirrored back to GitHub. Your commits are in it, under your name, and GitHub shows the pull request as
   **Merged**. If the maintainers had to squash or rebase, the pull request is closed with a link to the commit.

Why the detour: the project builds, signs and publishes releases from GitLab, and having one place where code is merged keeps both hosts identical.
Why a maintainer label first: the build runners there are privileged, so nobody's code runs on them before a person has read it.

## Releases

Releases and nightly builds are made by CI from `main`; see [runbooks/release.md](runbooks/release.md). You never need to touch version numbers.

## Secrets

CI scans the whole history for secrets with [gitleaks](https://github.com/gitleaks/gitleaks) (`.gitleaks.toml`). Never commit real keys, tokens or
passwords, even in tests: use obvious placeholders. A value that only looks like a secret (an example token, a test certificate placeholder) can be
marked with a `gitleaks:allow` comment on its line. Findings already reviewed are in `.gitleaksignore`. If you committed a real secret, tell a
maintainer: it must be revoked, not just deleted.
