# 0208. GitHub as the front door, GitLab as the merge point

Status: Accepted; review-label reuse superseded by [0212](0212-reviewed-commits-and-verified-archive-bytes.md)
Date: 2026-10-13

## Context

The project lives on GitLab (self-hosted) and GitHub, and we want the community to propose features and fixes as pull requests, with a merged change
appearing on both hosts. Two hosts that both accept merges diverge sooner or later (a force push, a squash, a rebase on one side), and then
nobody knows which history is right. Releases are signed and published from one host (ADR 0206), which also needs one source of truth.

## Decision

- **One place merges.** GitLab. GitHub receives `main` and tags from a GitLab **push mirror** (protected branches and tags only); nobody pushes to
  GitHub `main` except that mirror.
- **GitHub is where people arrive:** issues, pull requests, discussion, security reports. Checks on a pull request (`ci.yml`, `dco.yml`) run in
  GitHub's sandbox, on the `pull_request` event, with no secrets.
- **A maintainer's label forwards a pull request.** Adding `ready-for-gitlab` triggers `bridge.yml` (`pull_request_target`), which fetches the pull
  request's commits **as data** (never checks out, builds or runs them), pushes them unchanged to GitLab as `github/pr-<n>`, and opens or updates a merge
  request there (labelled `from-github`, no squash, source branch removed on merge, with a `GitHub-PR:` reference). New commits on the pull request
  follow; closing the pull request without merging closes the merge request. The label is the review gate: GitLab's runners are privileged, so no
  stranger's code reaches them before a person has read it, and protected CI variables (the signing keys) are never exposed to `github/pr-*` branches.
- **Contributors keep their commits.** The merge request is merged with a merge commit or fast-forward, so the same commits arrive on `main` and
  GitHub marks the pull request **Merged** by itself. After a squash or rebase the `merged.yml` workflow finds `Merge branch 'github/pr-<n>'` in the
  mirrored history and closes the pull request with a comment.
- **DCO, not a CLA.** Every commit needs `Signed-off-by:` with the author's or committer's email (`tools/bridge/dco.py`, run by `dco.yml`; merge commits and
  bots are exempt). The licence is Apache-2.0 (ADR 0200), which already grants what a CLA would ask for.
- **Logic in scripts.** `tools/bridge/bridge.py` and `dco.py` are standard-library Python with unit tests (the git operations are tested against local
  repositories, the GitLab calls against a fake); the workflows only call them.

## Alternatives considered

- **Two-way sync of both hosts.** Rejected: two writers means conflicts, and a diverged branch stops the sync exactly when it matters.
- **GitHub as the merge point, GitLab as a mirror.** Simpler for contributors, but it moves all maintainer work and the existing automation from merge
  requests to pull requests, and the project already builds and publishes on GitLab. Possible later if GitLab is dropped; the pieces here would be removed.
- **Forward every pull request automatically.** Rejected for the runner exposure above.
- **A GitHub Action that merges the pull request on GitHub and pushes to GitLab.** Makes GitHub the merge point with extra moving parts and no review on GitLab.

## Consequences

- A change from outside takes one extra hop (label, then merge on GitLab). The contributor sees it in comments and does not need a GitLab account.
- The GitLab token that the bridge uses can create branches and merge requests; it is a secret on the GitHub repository. The workflow must never
  execute pull request content: that rule is in the workflow header and the runbook, and a test asserts the checkout is of the base branch only.
- Maintainers do not use GitHub's merge button; the ruleset on `main` makes the mirror the only writer.
- Setup steps and troubleshooting: [runbooks/github-front-door.md](../runbooks/github-front-door.md). Contributor guide: [contributing.md](../contributing.md).
