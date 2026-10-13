#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Developer Certificate of Origin check for the commits of a pull request (ADR 0208). Standard library only.

Every commit must carry a ``Signed-off-by: Name <email>`` line whose email is the commit's author or committer
email. That is the DCO (https://developercertificate.org): the contributor states they have the right to submit
the work under the project's licence. Merge commits and commits by bots are exempt.

    dco.py --range BASE..HEAD        # exit 1 and a Markdown report when a commit lacks a sign-off
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

_SIGNOFF = re.compile(r"^Signed-off-by:\s*(.+?)\s*<([^<>\s]+)>\s*$", re.MULTILINE | re.IGNORECASE)
_BOT = re.compile(r"(\[bot\]|^dependabot|^renovate)", re.IGNORECASE)
_SEP, _END = "\x1f", "\x1e"


@dataclass(frozen=True)
class Commit:
    """What the check needs from one commit."""

    sha: str
    author_name: str
    author_email: str
    committer_email: str
    parents: int
    message: str


def parse_log(raw: str) -> list[Commit]:
    """Parse ``git log --format=%H%x1f%an%x1f%ae%x1f%ce%x1f%P%x1f%B%x1e``."""
    commits = []
    for record in raw.split(_END):
        if not record.strip():
            continue
        sha, name, author, committer, parents, message = record.strip("\n").split(_SEP, 5)
        commits.append(Commit(sha, name, author, committer, len(parents.split()), message))
    return commits


def problems(commits: list[Commit]) -> list[str]:
    """One line per commit that is not signed off properly (empty: all good).

    Example:
        >>> ok = Commit("a" * 40, "Ann", "ann@example.org", "ann@example.org", 1,
        ...             "Fix it\\n\\nSigned-off-by: Ann <ann@example.org>")
        >>> bad = Commit("b" * 40, "Bob", "bob@example.org", "bob@example.org", 1, "Fix it")
        >>> problems([ok, bad])
        ['bbbbbbb: no Signed-off-by line']
    """
    out = []
    for c in commits:
        if c.parents > 1 or _BOT.search(c.author_name) or _BOT.search(c.author_email):
            continue
        emails = {m[2].lower() for m in _SIGNOFF.finditer(c.message)}
        if not emails:
            out.append(f"{c.sha[:7]}: no Signed-off-by line")
        elif not emails & {c.author_email.lower(), c.committer_email.lower()}:
            out.append(f"{c.sha[:7]}: Signed-off-by does not match the author ({c.author_email})")
    return out


def report(found: list[str]) -> str:
    """The Markdown for the pull request check: what failed and how to fix it."""
    lines = "\n".join(f"- `{p.split(':', 1)[0]}`:{p.split(':', 1)[1]}" for p in found)
    return (
        "### Developer Certificate of Origin\n\n"
        "Every commit needs a `Signed-off-by: Your Name <you@example.org>` line using the email of its author "
        "([what it means](https://developercertificate.org)). Missing:\n\n"
        f"{lines}\n\n"
        "To fix the last commit: `git commit --amend --signoff --no-edit`. For several: "
        "`git rebase --signoff <base>` and then `git push --force-with-lease`. Next time, commit with `git commit -s`.\n"
    )


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--range", required=True, help="BASE..HEAD as commit ids or refs")
    p.add_argument("--repo-dir", default=".")
    a = p.parse_args(argv)
    if not re.fullmatch(r"[0-9A-Za-z._/~^-]+\.\.[0-9A-Za-z._/~^-]+", a.range):
        print("error: --range is BASE..HEAD", file=sys.stderr)
        return 2
    raw = subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            a.repo_dir,
            "log",
            f"--format=%H{_SEP}%an{_SEP}%ae{_SEP}%ce{_SEP}%P{_SEP}%B{_END}",
            a.range,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = problems(parse_log(raw))
    if found:
        print(report(found))
        return 1
    print("All commits are signed off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
