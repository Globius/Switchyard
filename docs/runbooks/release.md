# Nightly builds and releases

Who: maintainers. What: how a change becomes a nightly build, how we cut `0.1.0`, `1.0.0` or a release candidate, and how the signing keys
are made and protected. Design: [ADR 0206](../adr/0206-nightly-and-release-builds.md) (and [0205](../adr/0205-upgrades-versions-bundles-backups.md)
for the bundles and feeds). Operator side: [upgrades.md](../upgrades.md).

## How it works

```text
merge to main ──► nightly job (03:00 UTC, if something changed) ──► 0.2.0-nightly.20261014, signed with the NIGHTLY key
                                                                  └─► nightly.json (newest 14), channel "nightly"
tag v0.2.0 ──────► release job ──► 0.2.0, signed with the RELEASE key ──► bundle + releases.json + SBOMs, channel "stable"
tag v0.2.0-rc1 ──► the same, channel "candidate"
```

- `VERSION` (repo root) is the version **under development**. Nightlies are `<VERSION>-nightly.<date>`. A release tag must name `VERSION`
  (`v0.2.0` needs `VERSION` = `0.2.0`), or the job refuses before building.
- On GitLab the publish job waits only for `lint`, `typecheck`, `test` and `ui` (not the long `test-full`), so tag a commit whose main pipeline you have seen go green. Both GitLab and GitHub run the same scripts (`tools/release/`). Only one host publishes: the CI variable `RELEASE_HOST` (`gitlab` by default,
  `github` to switch). The other host still runs every check.

## Cut a release

1. Make sure `main` is green on the host that publishes and `VERSION` is what you are releasing.
2. Write `docs/release-notes/<version>.json` ([README](../release-notes/README.md)): `breaking` and `action_required` are shown to operators before
   they confirm. Set `COMPAT.recommended_min_agent` / `min_agent` in `controller/switchyard/services/upgrade.py` if this release makes older
   agents second-class. Merge.
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0` (a release candidate: `v0.2.0-rc1`). Tags are protected, so only maintainers can.
4. The pipeline builds the images, chart, agent and bundle, signs, publishes, extends `releases.json` and creates the release. On GitHub the
   `release` environment needs a reviewer's approval first.
5. Check from a controller: Admin > Upgrades > Check now shows the version, its notes and the install path.
6. Set `VERSION` to the next development version in a commit on `main` (`0.2.1` or `0.3.0`) and run `python3 tools/release/version.py stamp <it>`
   locally only if you want your checkout to match; CI stamps its own copy.

`UPGRADE_FROM` (a CI variable, space separated) lists the versions a release may be installed over; leave it empty for "from any". A site further
behind then gets the chain of releases to go through.

## Nightlies

They run from a pipeline schedule (GitLab: CI/CD > Schedules, daily, branch `main`, variable `NIGHTLY=1`) or the cron in `release.yml` (GitHub).
Run one by hand: GitLab, Build > Pipelines > Run pipeline on `main` with variables `NIGHTLY=1` (and `FORCE_NIGHTLY=1` when nothing changed); GitHub, the "force nightly" input of the release workflow. They are signed with the nightly key and are
only seen by installations that chose the `nightly` channel **and** trust that key.

## Setting it up once

1. **Keys.** Three, made on a trusted machine (not CI, not a controller). With the repo checked out:
   `syctl release keygen <file> --key-id <id>`. With only Docker:

   ```sh
   docker run --rm -v "$PWD:/k" alpine/openssl genpkey -algorithm ed25519 -out /k/ci-release-2026.pem
   docker run --rm -v "$PWD:/k" alpine/openssl pkey -in /k/ci-release-2026.pem -pubout
   ```

   (PowerShell: `${PWD}` instead of `$PWD`.) The second command prints the public key.

   | Key | File | Lives in | Signs | Trusted by |
   | --- | --- | --- | --- | --- |
   | `release-2026` | `release-2026.pem` | offline only (a hardware token or a locked vault entry, and a second copy) | manual or emergency releases; the fallback | every controller (`PROJECT_KEYS`) |
   | `ci-release-2026` | `ci-release-2026.pem` | the CI release secret, then deleted from your disk | tagged releases | every controller (`PROJECT_KEYS`) |
   | `nightly-2026` | `nightly-2026.pem` | the CI nightly secret, then deleted from your disk | nightlies | only installations that add it themselves |

   The three public keys are listed in [keys.md](../keys.md). Splitting them means a CI compromise can be undone: drop `ci-release-2026` from `PROJECT_KEYS` in the next release (signed with the offline
   key) and new installs stop trusting it, while the offline key never touched CI.
2. (Done for `release-2026` and `ci-release-2026`.) Put public halves in `PROJECT_KEYS` (`controller/switchyard/services/release_feed.py`; the value is
   the 32 raw bytes in base64, which is the last 32 bytes of the DER from `-pubout`) and merge. Publish all three public keys in the release notes
   so sites that pin keys can. **Never** put the nightly key in `PROJECT_KEYS`.
3. Store the two CI private keys on **the host that publishes** (the other host needs no signing key), as base64 on one line, then delete the files.
   The value: `base64 -w0 ci-release-2026.pem` (macOS: `base64 -i ci-release-2026.pem | tr -d '\n'`; PowerShell:
   `[Convert]::ToBase64String([IO.File]::ReadAllBytes("ci-release-2026.pem"))`).
   - **GitLab** (Settings > CI/CD > Variables > Add variable; type *Variable*, *Protect variable* on, *Mask variable* on, *Expand variable
     reference* off):

     | Key | Value |
     | --- | --- |
     | `RELEASE_SIGNING_KEY_B64` | base64 of `ci-release-2026.pem` |
     | `RELEASE_KEY_ID` | `ci-release-2026` (protected, not masked) |
     | `NIGHTLY_SIGNING_KEY_B64` | base64 of `nightly-2026.pem` |
     | `NIGHTLY_KEY_ID` | `nightly-2026` (protected, not masked) |

     Then Settings > Repository > Protected tags: wildcard `v*`, allowed to create: Maintainers. `main` is protected already; protected variables are
     only passed to protected branches and tags. Last, Build > Pipeline schedules > New: `0 3 * * *`, target `main`, variable `NIGHTLY` = `1`.
   - **GitHub**, only if GitHub publishes (set the repository variable `RELEASE_HOST` = `github` under Settings > Secrets and variables > Actions >
     Variables): Settings > Environments > New environment `release`, with *Deployment branches and tags* limited to the tag `v*`, *Required
     reviewers* set to you, and secrets `RELEASE_SIGNING_KEY_B64` and `RELEASE_KEY_ID`. A second environment `nightly`, limited to branch `main`,
     with `NIGHTLY_SIGNING_KEY_B64` and `NIGHTLY_KEY_ID`. Add a tag ruleset (Settings > Rules) so only maintainers can create `v*` tags.
4. The project (or at least its package registry and releases) must be readable without a login, because controllers fetch the feed and bundles
   anonymously. Feeds: GitLab `https://<gitlab>/api/v4/projects/<id>/packages/generic/switchyard-feed/latest/releases.json` (and `nightly.json`);
   GitHub `https://github.com/<org>/<repo>/releases/download/feed/releases.json`.
5. On the first run, check the GitLab feed upload: re-uploading a file with the same name to a generic package must return the newest file when
   downloaded. If your GitLab keeps the oldest, enable "Do not allow duplicates" off or publish the feed as a Pages file instead.

## Rotating or losing a key

Generate a new key, add its public half to `PROJECT_KEYS` **next to** the old one, release, and drop the old one after the last version that trusts
only it is out of support. For a leaked key: remove it from `PROJECT_KEYS` in the next release, say so in the notes, and tell operators who pinned
it. A leaked **nightly** key only affects installations that trust it: remove its secret, make a new one, and tell testers to replace the key.

## Do not

- Do not sign on a shared runner or print the key. The scripts write it to a 0600 temporary file and remove it on exit.
- Do not edit `releases.json` by hand; the signature covers it and controllers refuse the file.
- Do not reuse a version number or move a release tag.
- Do not accept unsigned bundles for production. `syctl release build --unsigned` and `syctl upgrade apply --allow-unsigned --confirm-unsigned
  <version>` exist for development; the file hashes are still checked but nobody vouches for the bundle.

## By hand (without CI)

```sh
python3 tools/release/version.py compute nightly            # or: compute release --tag v0.2.0
sh tools/release/build-artifacts.sh 0.2.0-nightly.20261014 dist   # needs docker, helm, go; stamps the working copy
RELEASE_SIGNING_KEY="$(cat nightly.pem)" RELEASE_KEY_ID=nightly-2026 sh tools/release/bundle.sh 0.2.0-nightly.20261014 dist
RELEASE_SIGNING_KEY="$(cat nightly.pem)" RELEASE_KEY_ID=nightly-2026 \
  sh tools/release/feed.sh dist/switchyard-0.2.0-nightly.20261014.sybundle https://example.com/b.sybundle dist/nightly.json
```
