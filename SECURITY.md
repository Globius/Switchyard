# Security policy

## Reporting a vulnerability

Please do **not** open a public issue or pull request for a security problem. Use GitHub's private reporting instead: on the repository page open
**Security > Report a vulnerability**. That opens a private conversation with the maintainers.

Please include: what you found and where, the version (`syctl version`, or Admin > Upgrades), how to reproduce it, and what you think the impact is.
Remove real secrets, tokens and addresses from anything you send.

We aim to acknowledge a report within a few working days, tell you whether we agree it is a vulnerability, and agree a date for publishing the fix
and a description (crediting you if you wish). Please keep the details private until then.

## What is in scope

The controller (API, worker, CLI, web UI), the engine agent `sy-agent` and the engine image, the Helm chart and deployment files, and the signed
release and upgrade process (bundles, feeds, keys). Examples we care most about: crossing a tenant boundary, bypassing authentication or the
permission matrix, writing outside `/etc/switchyard/` or running a command through the agent, getting a config snippet past the validators and
renderer, leaking a secret, and getting an unsigned or wrongly signed bundle accepted.

## Supported versions

Switchyard is pre-1.0: only the latest release gets fixes. Nightly builds are not supported.

## How releases are signed

Public keys for verifying releases are listed in [docs/keys.md](docs/keys.md). If you are given a key that is not listed there, do not trust it.
