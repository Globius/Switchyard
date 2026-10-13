# 0206. Nightly builds, releases and CI on two hosts

Status: Accepted
Date: 2026-10-13

## Context

ADR 0205 built signed upgrade bundles and a signed release feed, but nothing produced them, the version was a literal in four files, and
the project lives on both GitLab and GitHub with CI on only one of them. We want every change to roll into a nightly build that testers can
install, releases cut when we are ready (`0.1.0`, `1.0.0`, release candidates), and the same checks and the same release logic on both hosts.

## Decision

- **Trunk-based, no release branches.** Every change merges to `main` through a merge or pull request. Nothing else about how the repo
  works changes, except two things below.
- **`VERSION`** (repository root) holds the version under development, `MAJOR.MINOR.PATCH`. It is the one place a version is set.
  `tools/release/version.py` derives the others and stamps them into the build's working copy (never committed): the controller
  (`__version__` and the PEP 440 form in `pyproject.toml`), the chart (`version`, `appVersion`), and the agent (a linker flag). A test fails if
  the files disagree with `VERSION`.
- **Nightly:** a scheduled job on `main` builds `<VERSION>-nightly.<YYYYMMDD>`, skipped when nothing was committed in the last 25 hours.
  It publishes a bundle and adds it to `nightly.json` (the newest 14 are kept). It is signed with the **nightly key**.
- **Release:** a tag `vX.Y.Z` (or `vX.Y.Z-rcN`) whose numbers equal `VERSION` builds that version, signs it with the **release key**, publishes
  the bundle and extends `releases.json`, and creates the host's release with the SBOMs. After tagging, a maintainer sets `VERSION` to the next
  development version. A tag that does not match `VERSION` fails before anything is built.
- **Channels:** `stable` (plain versions), `candidate` (`-rcN`), `nightly`. A feed lists any of them; the controller's setting picks which it
  looks at, and each wider channel includes the narrower ones. Versions order by semantic-version precedence
  (`0.2.0-nightly.20261014` < `0.2.0-rc1` < `0.2.0` < `0.2.1-nightly.1`), implemented once in `domain/versions.py` and used by the update
  check, the feed, the install path and the bundle plan.
- **Three signing keys, three trust levels.** An offline maintainer key (`release-2026`), the key CI signs tagged releases with (`ci-release-2026`) and the nightly key. The first two are in `PROJECT_KEYS`; the nightly key is not, so no installation trusts it unless an operator adds its
  public key to the feed settings. A leaked nightly key harms only opted-in test systems. The release key is in `PROJECT_KEYS`.
  Each key sits in its own protected CI secret, available only to the job that needs it.
- **Unsigned bundles are never accepted by default.** `syctl release build --unsigned` makes one for development, and `syctl upgrade apply
  --allow-unsigned --confirm-unsigned <version>` installs it, still checking every file hash, after a warning and with the version typed back.
  There is no setting for it in the UI and the controller's own staging refuses unsigned bundles.
- **Same logic on both hosts.** Everything that matters is in `tools/release/*.sh` and `version.py`: `build-artifacts.sh`, `bundle.sh`, `feed.sh`.
  `.gitlab-ci.yml` and `.github/workflows/{ci,release}.yml` only supply a runner and the publishing step. `ci.yml` runs the same checks as
  `.gitlab-ci.yml` job for job (and the UI check is new on both).
- **One host publishes.** The CI variable `RELEASE_HOST` (`gitlab` by default, or `github`) decides; the other host still runs the checks.
  Otherwise a tag would produce two different signed bundles. Feeds live in one fixed place per host (GitLab: Package Registry
  `switchyard-feed/latest/`; GitHub: the rolling release `feed`). GitLab can also build and sign, then copy the bundle and feed to GitHub
  Releases when `GITHUB_REPO` and `GITHUB_RELEASE_TOKEN` are set, which is how a private GitLab serves a public feed (release runbook).

## Consequences

- Bundles from CI hold the images, the chart, the agent and the Compose file. The SE template is still built by hand
  ([build-se-template.md](../runbooks/build-se-template.md)); the agent inside it should now be built with `SY_VERSION` so engines report a
  real version (`se-image/fetch-binaries.sh`).
- The release key lives in CI. That is a deliberate trade for automated releases; protection is the protected variable (GitLab) or the
  environment with required reviewers (GitHub), tags that only maintainers can push, and the ability to revoke by dropping the key from
  `PROJECT_KEYS`. The offline path in the runbook still works.
- The feed and bundle URLs must be reachable by controllers without credentials, so the project (or at least the package registry and
  releases) must be public.
- The GitHub workflows have not been run on GitHub yet. The GitLab ones are checked by the merge request pipeline except the publishing
  jobs, which only run on a schedule or a tag.
