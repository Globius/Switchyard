# GitHub as the front door: mirror, pull request bridge, DCO

Who: a maintainer with admin rights on both the GitLab project and the GitHub repository. Design: [ADR 0208](../adr/0208-github-front-door.md).
Contributor view: [contributing.md](../contributing.md).

```text
 community ──PR──► GitHub ──review──► label "ready-for-gitlab" ──bridge──► GitLab MR "github/pr-123" ──merge──► GitLab main
                     ▲                                                                                              │
                     └─────────────────────── push mirror (main + tags) ────────────────────────────────────────────┘
```

GitLab is the only place code is merged and releases are built. GitHub gets `main` and tags from a GitLab push mirror, runs its own checks on pull
requests in its sandbox, and forwards a pull request to GitLab only after a maintainer has read it and added the label.

## One time: GitLab

1. **Merge method** (Settings > Merge requests): *Merge commit with semi-linear history* or *Fast-forward merge*, and squash set to *Do not allow* (or
   at most *Allow*). A squash replaces the contributor's commits with a new one, and GitHub then cannot see the pull request as merged. (The merged
   workflow closes it with a comment if you squash anyway.) Keep *Delete source branch* on, so `github/pr-*` branches go away.
2. **Bridge token** (Settings > Access tokens): name `github-bridge`, role **Developer**, scopes `api` and `write_repository`, an expiry you will
   remember. Developer is enough to create branches and merge requests; it cannot touch protected `main`. Copy the token once.
3. **Push mirror** (Settings > Repository > Mirroring repositories): URL `https://github.com/<org>/<repo>.git`, direction **Push**, the *password* is a
   GitHub fine-grained token limited to this one repository with *Contents: Read and write* (and, if it will push workflow files, *Workflows*),
   username any, **Only mirror protected branches** on (so `github/pr-*` are not sent back), and keep *Keep divergent refs* off. Tags are mirrored.
4. Protected branches: `main` protected as now; protect tags `v*`. Run **Update now** on the mirror once and check GitHub shows the history.

## One time: GitHub

1. **Repository variables** (Settings > Secrets and variables > Actions > Variables): `GITLAB_URL` (`https://git.example.org`), `GITLAB_PROJECT` (the
   numeric project id, shown under the project name on GitLab), `GITLAB_PROJECT_PATH` (`group/project`). **Secret**: `GITLAB_TOKEN` (the bridge token).
2. **Label**: create `ready-for-gitlab` (Issues > Labels). Only people with triage access or more can add it, which is who you want.
3. **Forks** (Settings > Actions > General): *Fork pull request workflows from outside collaborators* = **Require approval for all outside
   collaborators**. The workflows on the `pull_request` event run in GitHub's sandbox with no secrets, but approval stops people burning your minutes.
4. **Protect `main`** with a ruleset: block direct pushes and force pushes, require the `dco` and `ci` checks on pull requests, and add the account whose
   token the GitLab mirror uses to the **bypass list**, so the mirror is the only writer. Nobody merges pull requests with GitHub's button: the
   *Merge* button should be unavailable (ruleset: restrict merging, or just tell maintainers). The bridge is how a pull request lands.
5. Turn on **Issues**, **Private vulnerability reporting** (Settings > Code security), and optionally Discussions. Issues live on GitHub only; if you
   have GitLab issues you still want, say so in your issue templates' contact links.
6. Leave the repository variable `RELEASE_HOST` unset or `gitlab`: releases are built only on GitLab ([release.md](release.md)).

## Trying it

1. Open a pull request from a fork (or a branch) with a commit that has `Signed-off-by`. `ci` and `dco` run on GitHub.
2. Add the label `ready-for-gitlab`. Within a minute the `bridge` workflow comments with a merge request link; on GitLab there is a branch
   `github/pr-<n>` with the author's commits and an MR titled `[GitHub #<n>] ...`, labelled `from-github`, with its pipeline running.
3. Merge the MR on GitLab. Within minutes the mirror pushes `main`; the pull request on GitHub turns purple (**Merged**). With a squash it is closed by
   the `merged` workflow with a comment.
4. Push another commit to the pull request and check the GitLab branch stays at the reviewed commit. Review the new diff and add
   `ready-for-gitlab` again; only then should the MR pick it up. Close a pull request without merging and check its MR is closed.

## Rules for maintainers

- **Read the diff before you label.** The label is what lets the code reach GitLab's privileged runners (`docker-privileged`). Look especially at
  `.gitlab-ci.yml`, `tools/`, `deploy/`, `Dockerfile`s and anything that runs in tests. If a change to CI files is needed, a maintainer should make it.
- **Review each revision.** The bridge consumes the label for the head commit captured by that label event. New commits and reopened pull requests
  require a fresh review and label; they never forward automatically. If the head moves before the bridge fetches it, forwarding fails without
  updating GitLab. Review the new head and label it again ([ADR 0212](../adr/0212-reviewed-commits-and-verified-archive-bytes.md)).
- The bridge never builds or runs the pull request: it fetches the commits (`refs/pull/<n>/head`) as data and pushes them. Keep it that way; do not add
  a step to `bridge.yml` that checks out or runs the pull request's code.
- Protected CI variables (the signing keys) are not available to `github/pr-*` branches, because those branches are not protected. Do not protect that
  pattern.
- Contributors keep authorship. Do not rewrite their commits when merging; ask them to rebase if the MR has conflicts.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| No comment after labelling | `GITLAB_URL` variable unset (the job is skipped); or the label was added on a closed pull request |
| `GitLab answered 401/403` | the bridge token expired, lost its scope, or the role is below Developer |
| `git push failed` | the token lacks `write_repository`; or a branch `github/pr-<n>` exists protected |
| `the pull request changed after review` | a newer head arrived before the bridge fetched the approved commit; review the new head and add the label again |
| MR opened but no pipeline | MR pipelines are disabled for the project, or the pipeline needs a runner approval |
| PR stays open after the MR merged | the merge was a squash and the `merged` workflow did not run: check the mirror pushed `main`, and that the merge commit subject names `github/pr-<n>` |
| Mirror fails with "protected branch" | the GitHub ruleset blocks the mirror account: add it to the bypass list |
| `dco` fails on a bot's commit | bots named `[bot]`, `dependabot` and `renovate` are exempt; add other bots to the list in `tools/bridge/dco.py` |
