#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Forward a GitHub pull request to GitLab as a merge request, and close it again (ADR 0208). Standard library only.

GitLab is where code is merged; GitHub is where the community arrives. A maintainer reviews a pull request
on GitHub and labels it ``ready-for-gitlab``. This script then pushes the pull request's commits, unchanged,
to GitLab as the branch ``github/pr-<n>`` and opens (or updates) a merge request for it. Approval covers
only the head commit in that label event; every new revision needs a new review and label. Merging that merge
request with a merge commit or fast-forward keeps the contributor's commits, so GitHub shows the pull request
as Merged once the GitLab push mirror brings ``main`` back. After a squash or rebase GitHub cannot tell, so
``merged`` finds the pull requests that landed and the workflow closes them with a comment.

Nothing here runs or builds the pull request's code: it only fetches the pull request's commits as data.

    bridge.py sync   --event event.json       # needs GITLAB_URL, GITLAB_PROJECT, GITLAB_TOKEN
    bridge.py close  --event event.json
    bridge.py merged --range BEFORE..AFTER    # prints the numbers of pull requests merged on GitLab
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LABEL = "ready-for-gitlab"
MR_LABEL = "from-github"
_BRANCH = re.compile(r"github/pr-(\d+)$")
_MERGE_SUBJECT = re.compile(r"Merge branch 'github/pr-(\d+)' into")


class BridgeError(Exception):
    """Something the bridge cannot do. The message is safe to post as a comment."""


def branch_for(number: int) -> str:
    """The GitLab branch for pull request ``number``.

    Example:
        >>> branch_for(12)
        'github/pr-12'
    """
    return f"github/pr-{int(number)}"


def number_from_branch(name: str) -> int | None:
    """The pull request number in a bridge branch name, or ``None``.

    Example:
        >>> number_from_branch("github/pr-12"), number_from_branch("feature/x")
        (12, None)
    """
    m = _BRANCH.search(name)
    return int(m[1]) if m else None


def merged_numbers(subjects: list[str]) -> list[int]:
    """Pull request numbers named by GitLab's default merge-commit subjects.

    Example:
        >>> merged_numbers(["Merge branch 'github/pr-7' into 'main'", "Fix a thing"])
        [7]
    """
    return [int(m[1]) for s in subjects if (m := _MERGE_SUBJECT.match(s))]


@dataclass(frozen=True)
class Pr:
    """The parts of a pull request the bridge needs."""

    number: int
    title: str
    body: str
    url: str
    author: str
    repo: str
    base: str
    state: str
    merged: bool
    head_sha: str
    action: str
    added_label: str
    labels: tuple[str, ...] = field(default=())

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "Pr":
        """Read a ``pull_request`` or ``pull_request_target`` event payload."""
        pr = event["pull_request"]
        return cls(
            number=int(pr["number"]),
            title=str(pr["title"]),
            body=str(pr.get("body") or ""),
            url=str(pr["html_url"]),
            author=str(pr["user"]["login"]),
            repo=str(event["repository"]["full_name"]),
            base=str(pr["base"]["ref"]),
            state=str(pr["state"]),
            merged=bool(pr.get("merged")),
            head_sha=str(pr.get("head", {}).get("sha", "")),
            action=str(event.get("action", "")),
            added_label=str(event.get("label", {}).get("name", "")),
            labels=tuple(str(x["name"]) for x in pr.get("labels", [])),
        )


def mr_title(pr: Pr) -> str:
    """``[GitHub #12] Title``, cut to GitLab's limit."""
    return f"[GitHub #{pr.number}] {pr.title}"[:255]


def mr_description(pr: Pr) -> str:
    """The merge request text: where it came from, the author's words, and a stable reference."""
    return (
        f"Forwarded from GitHub: {pr.url}\n\nAuthor: @{pr.author}\n\n---\n\n{pr.body.strip()}\n\n"
        f"GitHub-PR: {pr.repo}#{pr.number}\n"
    )[:100_000]


Http = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]


def _urllib_http(method: str, url: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 (https URL from config)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310
            raw = res.read()
            return res.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None


class GitLab:
    """The few GitLab API calls the bridge makes."""

    def __init__(self, base_url: str, project: str, token: str, http: Http = _urllib_http) -> None:
        """`project` is the numeric id or the url-encoded path; `http` is replaced in tests."""
        if not base_url.startswith("https://"):
            raise BridgeError("GITLAB_URL must be https")
        self._api = f"{base_url.rstrip('/')}/api/v4/projects/{urllib.parse.quote(project, safe='')}"
        self._headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        self._http = http

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        status, data = self._http(method, f"{self._api}{path}", self._headers, body)
        if status >= 400:
            message = data.get("message") if isinstance(data, dict) else None
            raise BridgeError(
                f"GitLab answered {status} for {method} {path.split('?')[0]}: {message}"
            )
        return data

    def find_mr(self, branch: str) -> dict[str, Any] | None:
        """The open merge request from ``branch``, if any."""
        q = urllib.parse.urlencode({"source_branch": branch, "state": "opened"})
        found = self._call("GET", f"/merge_requests?{q}")
        return found[0] if found else None

    def create_mr(self, branch: str, target: str, title: str, description: str) -> dict[str, Any]:
        """Open a merge request that deletes its source branch and keeps the commits (no squash)."""
        return self._call(
            "POST",
            "/merge_requests",
            {
                "source_branch": branch,
                "target_branch": target,
                "title": title,
                "description": description,
                "labels": MR_LABEL,
                "remove_source_branch": True,
                "squash": False,
                "allow_collaboration": False,
            },
        )

    def update_mr(self, iid: int, title: str, description: str) -> dict[str, Any]:
        """Refresh the title and text of an open merge request."""
        return self._call(
            "PUT", f"/merge_requests/{int(iid)}", {"title": title, "description": description}
        )

    def close_mr(self, iid: int) -> None:
        """Close a merge request without merging."""
        self._call("PUT", f"/merge_requests/{int(iid)}", {"state_event": "close"})


def push_pull_request(
    repo_dir: str,
    number: int,
    gitlab_git_url: str,
    token: str,
    expected_head: str,
    remote: str = "origin",
) -> str:
    """Fetch the pull request's commits from GitHub and push them to GitLab; returns the head commit.

    The token travels in the environment as an HTTP header for this one command, never in the URL or
    the arguments, so it is not in the process list or in ``.git/config``.
    """
    number = int(number)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise BridgeError("the review event has no valid head commit")
    ref = f"refs/bridge/pr-{number}"
    run = _git(repo_dir)
    run(["fetch", "--no-tags", remote, f"+refs/pull/{number}/head:{ref}"])
    head = run(["rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()
    if head != expected_head:
        raise BridgeError("the pull request changed after review; review it and add the label again")
    basic = base64.b64encode(f"oauth2:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    # Push the immutable commit we checked, not a ref that could move after the check.
    run(["push", "--force", gitlab_git_url, f"{head}:refs/heads/{branch_for(number)}"], env)
    return head


def _git(repo_dir: str) -> Callable[..., str]:
    def run(args: list[str], extra_env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(  # noqa: S603 (fixed git binary, validated arguments)
            ["git", "-C", repo_dir, *args],  # noqa: S607
            capture_output=True,
            text=True,
            env={**os.environ, **(extra_env or {})},
            check=False,
        )
        if proc.returncode != 0:
            raise BridgeError(
                f"git {args[0]} failed: {proc.stderr.strip().splitlines()[-1:] or ['']}"
            )
        return proc.stdout

    return run


@dataclass(frozen=True)
class Result:
    """What a sync did, for the comment on the pull request."""

    action: str  # "created", "updated", "ignored" or "closed"
    mr_url: str = ""
    message: str = ""


def sync(pr: Pr, gl: GitLab, push: Callable[[], str], target: str = "main") -> Result:
    """Make GitLab match an approved pull request: push its commits, then open or refresh the merge request.

    Only adding ``ready-for-gitlab`` to an open pull request aimed at ``target`` approves a push.
    Synchronizing, reopening, or adding another label never reuses an earlier approval.
    """
    if pr.state != "open":
        return Result("ignored", message="the pull request is not open")
    if LABEL not in pr.labels:
        return Result("ignored", message=f"it has no `{LABEL}` label")
    if pr.base != target:
        return Result("ignored", message=f"it targets `{pr.base}`, not `{target}`")
    if pr.action != "labeled" or pr.added_label != LABEL:
        return Result("ignored", message=f"a new review and `{LABEL}` label are required")
    if not re.fullmatch(r"[0-9a-f]{40}", pr.head_sha):
        raise BridgeError("the review event has no valid head commit")
    push()
    branch = branch_for(pr.number)
    existing = gl.find_mr(branch)
    if existing:
        gl.update_mr(existing["iid"], mr_title(pr), mr_description(pr))
        return Result("updated", existing["web_url"])
    created = gl.create_mr(branch, target, mr_title(pr), mr_description(pr))
    return Result("created", created["web_url"])


def close(pr: Pr, gl: GitLab) -> Result:
    """Close the merge request of a pull request that was closed on GitHub without merging."""
    existing = gl.find_mr(branch_for(pr.number))
    if existing is None:
        return Result("ignored", message="no open merge request")
    gl.close_mr(existing["iid"])
    return Result("closed", existing["web_url"])


def _from_env() -> GitLab:
    try:
        return GitLab(
            os.environ["GITLAB_URL"], os.environ["GITLAB_PROJECT"], os.environ["GITLAB_TOKEN"]
        )
    except KeyError as exc:
        raise BridgeError(f"{exc.args[0]} is not set") from exc


def main(argv: list[str] | None = None) -> int:
    """Command line entry point; prints one JSON line the workflow reads."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "close"):
        s = sub.add_parser(name)
        s.add_argument("--event", required=True)
        s.add_argument("--repo-dir", default=".")
    m = sub.add_parser("merged")
    m.add_argument("--range", required=True)
    m.add_argument("--repo-dir", default=".")
    a = p.parse_args(argv)
    try:
        if a.cmd == "merged":
            if not re.fullmatch(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}", a.range):
                raise BridgeError("--range is BEFORE..AFTER as commit ids")
            subjects = _git(a.repo_dir)(["log", "--format=%s", a.range]).splitlines()
            print(json.dumps({"merged": merged_numbers(subjects)}))
            return 0
        with open(a.event, encoding="utf-8") as fh:
            pr = Pr.from_event(json.load(fh))
        gl = _from_env()
        if a.cmd == "close":
            result = close(pr, gl)
        else:
            url = os.environ["GITLAB_URL"].rstrip("/")
            git_url = f"{url}/{os.environ.get('GITLAB_PROJECT_PATH', '')}.git"
            result = sync(
                pr,
                gl,
                lambda: push_pull_request(
                    a.repo_dir, pr.number, git_url, os.environ["GITLAB_TOKEN"], pr.head_sha
                ),
            )
        print(json.dumps(result.__dict__))
        return 0
    except BridgeError as exc:
        print(json.dumps({"action": "error", "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
