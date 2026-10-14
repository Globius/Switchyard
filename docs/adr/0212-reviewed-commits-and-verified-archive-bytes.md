# 0212. Bind approvals to reviewed commits and verification to consumed archive bytes

Status: Accepted
Date: 2026-10-13

## Context

The contribution bridge sends approved GitHub commits to GitLab's privileged runners. Keeping its approval label across new commits made the
review apply to code the maintainer had never seen. Fetching the current pull-request ref also allowed the head to move between review and forwarding.

Upgrade bundles were verified and then reopened for extraction without checking the extracted bytes. A local process able to replace or modify
the bundle during that interval could substitute unsigned image bytes before a privileged upgrade loaded them.

FTP backup operations omitted the archive-name check used by other targets. The shared name matcher also accepted a final newline. Local backups
used predictable temporary paths, allowing an existing symlink at that path to redirect a write when another actor could write to the backup directory.

## Decision

- A `ready-for-gitlab` label event approves only its recorded head SHA. The bridge consumes the label before forwarding. Synchronization and reopening
  events invalidate old labels and never forward code. After fetching the pull request, the script compares its commit with the event SHA and pushes
  the checked immutable commit. A changed head fails closed and needs another review. This supersedes review-label reuse in ADR 0208.
- Upgrade extraction hashes and counts the bytes it writes against the already verified manifest, and refuses substituted member types or sizes.
  Images are loaded only after all extracted files pass. Destination files are created exclusively.
- Every backup store validates the complete archive name before a read, write or delete. Local writes use exclusively created random temporary
  files with owner-only permissions, then atomically replace the archive.

## Consequences

Contributors need another maintainer review for every revision sent to GitLab. A force push during review can require another label even if the
earlier review was legitimate. Upgrade extraction does a second streaming digest calculation. Backup targets keep their existing protocols and settings.
Backup and upgrade directories remain operator-owned; these changes do not grant untrusted users write access to them.

## Alternatives considered

- Keeping a reusable approval label does not distinguish reviewed commits from later contributor changes.
- Comparing only the archive's timestamp or pathname does not prove the bytes extracted are the bytes whose signature was checked.
- Checking whether a predictable temporary file exists before writing leaves a symlink race; exclusive random creation removes that dependency.
