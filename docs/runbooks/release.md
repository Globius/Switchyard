# Cut a release: bundle, signed feed, project key

Who: a maintainer with the release signing key. Result: a signed upgrade bundle, a signed `releases.json` that controllers read, and
release notes. Design: [ADR 0205](../adr/0205-upgrades-versions-bundles-backups.md), operator side: [upgrades.md](../upgrades.md).

## Once: the project signing key

Do this on a machine you trust, **not** in CI and not on a controller. The private key signs every release; anyone who holds it can make
controllers recommend (and operators install) their code.

```sh
syctl release keygen release.pem --key-id release-2026
# private key written to release.pem
# trust it with:  --trust release-2026=<base64 public key>
```

1. Store `release.pem` offline (a hardware token, or a password manager entry with restricted access). Back it up in a second place.
2. Put the printed **public** key in `PROJECT_KEYS` in `controller/switchyard/services/release_feed.py` (key id to base64 public key) and
   merge that change. Every controller of that version then trusts feeds and bundles signed with it, with no setting needed.
3. Publish the public key where operators can read it (the release notes and the project README), for sites that pin it themselves with
   `trusted_keys` or `--trust`.

Rotating: generate a new key, add it to `PROJECT_KEYS` **next to** the old one, release, and drop the old one after the last version
that trusts only it is out of support. A leaked key is removed in the next release and announced; operators who pinned it must change their pin.

## Each release

1. Version: set `__version__` (`controller/switchyard/__init__.py`), the agent `Version`, the chart `appVersion`, and raise
   `COMPAT.recommended_min_agent` / `min_agent` in `controller/switchyard/services/upgrade.py` if this release makes older agents second-class.
2. Build the artifacts: the controller and proxy images (`docker save`), `helm package deploy/helm/switchyard`, the Compose files, the `sy-agent`
   binary and the SE template ([build-se-template.md](build-se-template.md)).
3. Write `notes.json` (`new`, `deprecated`, `breaking`, `action_required`). **Breaking** and **action_required** are shown to the operator before
   they confirm an upgrade, so say what they must do.
4. Build and check the bundle:

   ```sh
   syctl release build switchyard-0.2.0.sybundle --key release.pem --key-id release-2026 --version 0.2.0 \
     --controller-image controller.tar --proxy-image proxy.tar --helm-chart switchyard-0.2.0.tgz \
     --compose compose.yaml --agent sy-agent --se-template se-template.tar \
     --upgrade-from 0.1.0 --min-agent 0.1.0 --recommended-min-agent 0.2.0 --notes notes.json
   syctl release verify switchyard-0.2.0.sybundle --trust release-2026=<public key>
   ```

   `--upgrade-from` lists the versions this release can be installed over. A site further behind gets the chain of releases to go through.
5. Publish the bundle (a GitLab or GitHub release asset, or any HTTPS location), then build the feed from the published URL. Keep the earlier
   feed and extend it so older releases stay listed:

   ```sh
   syctl release feed releases.json --key release.pem --key-id release-2026 --channel stable \
     --previous releases.json --bundle switchyard-0.2.0.sybundle=https://example.com/switchyard-0.2.0.sybundle
   ```

   It refuses a bundle signed with another key, a plain `http://` URL, and an earlier feed it cannot verify. Publish `releases.json` at the
   address operators put in Admin > Upgrades (a stable "latest release asset" URL works well). Use `--channel candidate` for a release candidate.
6. Check from a controller: Admin > Upgrades > Check now should show the new version, its notes and the install path.

## Do not

- Do not sign a feed or bundle on a shared CI runner. If you want CI to build the artifacts, let it stop at the unsigned files and sign on your machine.
- Do not edit `releases.json` by hand; the signature covers it and a controller will refuse the file.
- Do not reuse a version number. Controllers and the install-path logic key on it.
