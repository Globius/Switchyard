# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""tools/bridge: GitHub pull requests to GitLab merge requests, and the DCO check (ADR 0208)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"sy_{name}", REPO / f"tools/bridge/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


b = load("bridge")
d = load("dco")


def event(**over: Any) -> dict[str, Any]:
    pr = {
        "number": 12,
        "title": "Add a thing",
        "body": "Longer words.",
        "html_url": "https://github.com/org/switchyard/pull/12",
        "user": {"login": "ann"},
        "base": {"ref": "main"},
        "state": "open",
        "merged": False,
        "labels": [{"name": b.LABEL}],
    }
    pr.update(over)
    return {"pull_request": pr, "repository": {"full_name": "org/switchyard"}}


class PushSpy:
    """Stands in for the git push: remembers it was called and returns a commit id."""

    def __init__(self, sha: str) -> None:
        self.sha = sha
        self.calls: list[int] = []

    def __call__(self) -> str:
        self.calls.append(1)
        return self.sha


class FakeGitLab:
    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.existing = existing

    def http(self, method: str, url: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        self.calls.append((method, url, body))
        assert headers["PRIVATE-TOKEN"] == "t0ken"
        if method == "GET":
            return 200, [self.existing] if self.existing else []
        if method == "POST":
            return 201, {"iid": 5, "web_url": "https://gitlab.example/mr/5"}
        return 200, {"iid": 5, "web_url": "https://gitlab.example/mr/5"}

    def client(self) -> Any:
        return b.GitLab("https://gitlab.example", "group/switchyard", "t0ken", self.http)


def test_branch_names_round_trip() -> None:
    assert b.branch_for(12) == "github/pr-12"
    assert b.number_from_branch("github/pr-12") == 12 and b.number_from_branch("feature/x") is None
    assert b.merged_numbers(
        ["Merge branch 'github/pr-7' into 'main'", "Merge branch 'x' into 'main'"]
    ) == [7]


def test_the_merge_request_carries_the_origin_and_the_authors_words() -> None:
    pr = b.Pr.from_event(event())
    assert b.mr_title(pr) == "[GitHub #12] Add a thing"
    text = b.mr_description(pr)
    assert pr.url in text and "@ann" in text and "Longer words." in text
    assert text.rstrip().endswith("GitHub-PR: org/switchyard#12")
    long = b.Pr.from_event(event(title="x" * 400))
    assert len(b.mr_title(long)) == 255


def test_an_approved_pull_request_is_pushed_then_opened_as_a_merge_request() -> None:
    gitlab, spy = FakeGitLab(), PushSpy("abc")
    pushed = spy.calls
    result = b.sync(b.Pr.from_event(event()), gitlab.client(), spy)
    assert result.action == "created" and result.mr_url.endswith("/mr/5") and pushed == [1]
    method, url, body = gitlab.calls[-1]
    assert method == "POST" and url.endswith("/projects/group%2Fswitchyard/merge_requests")
    assert body["source_branch"] == "github/pr-12" and body["target_branch"] == "main"
    assert (
        body["squash"] is False and body["remove_source_branch"] is True
    )  # commits stay, branch goes
    assert body["labels"] == "from-github" and body["allow_collaboration"] is False


def test_a_second_sync_updates_the_same_merge_request() -> None:
    gitlab = FakeGitLab({"iid": 5, "web_url": "https://gitlab.example/mr/5"})
    result = b.sync(b.Pr.from_event(event(title="Better title")), gitlab.client(), lambda: "abc")
    assert result.action == "updated"
    method, url, body = gitlab.calls[-1]
    assert method == "PUT" and url.endswith("/merge_requests/5") and "Better title" in body["title"]
    assert not any(c[0] == "POST" for c in gitlab.calls)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"labels": []}, "label"),
        ({"state": "closed"}, "not open"),
        ({"base": {"ref": "release"}}, "targets"),
    ],
)
def test_nothing_is_pushed_unless_the_pull_request_is_open_labelled_and_aimed_at_main(
    over: dict[str, Any], reason: str
) -> None:
    gitlab, spy = FakeGitLab(), PushSpy("x")
    pushed = spy.calls
    result = b.sync(b.Pr.from_event(event(**over)), gitlab.client(), spy)
    assert result.action == "ignored" and reason in result.message
    assert pushed == [] and gitlab.calls == []


def test_a_closed_pull_request_closes_its_merge_request() -> None:
    gitlab = FakeGitLab({"iid": 5, "web_url": "https://gitlab.example/mr/5"})
    assert b.close(b.Pr.from_event(event(state="closed")), gitlab.client()).action == "closed"
    assert gitlab.calls[-1][2] == {"state_event": "close"}
    assert b.close(b.Pr.from_event(event()), FakeGitLab().client()).action == "ignored"


def test_gitlab_must_be_https_and_errors_are_reported_without_the_token() -> None:
    with pytest.raises(b.BridgeError, match="https"):
        b.GitLab("http://gitlab.example", "p", "t0ken")
    client = b.GitLab(
        "https://gitlab.example", "p", "t0ken", lambda *a: (403, {"message": "forbidden"})
    )
    with pytest.raises(b.BridgeError, match="403") as err:
        client.find_mr("github/pr-1")
    assert "t0ken" not in str(err.value)


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    import os

    out = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=Ann", "-c", "user.email=ann@example.org",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, env={**os.environ, **(env or {})},
    )  # fmt: skip
    return out.stdout.strip()


def test_the_pull_requests_commits_reach_gitlab_unchanged(tmp_path: Path) -> None:
    github, gitlab = tmp_path / "github", tmp_path / "gitlab.git"
    github.mkdir()
    git(github, "init", "-q", "-b", "main")
    (github / "a").write_text("1")
    git(github, "add", "a")
    git(github, "commit", "-q", "-m", "base")
    git(github, "checkout", "-q", "-b", "feature")
    (github / "b").write_text("2")
    git(github, "add", "b")
    git(github, "commit", "-q", "-s", "-m", "Add b")
    head = git(github, "rev-parse", "HEAD")
    git(
        github, "update-ref", "refs/pull/7/head", head
    )  # what GitHub exposes for every pull request
    git(github, "checkout", "-q", "main")
    subprocess.run(["git", "init", "-q", "--bare", str(gitlab)], check=True)

    sha = b.push_pull_request(str(github), 7, str(gitlab), "t0ken", remote=str(github))
    assert sha == head
    assert git(gitlab, "rev-parse", "refs/heads/github/pr-7") == head  # same commit, same author
    assert git(gitlab, "log", "-1", "--format=%an <%ae>", "github/pr-7") == "Ann <ann@example.org>"
    assert (
        "t0ken" not in (github / ".git" / "config").read_text()
    )  # the token never lands in the repo
    assert "Signed-off-by: Ann" in git(gitlab, "log", "-1", "--format=%B", "github/pr-7")
    # pushing again after the author force-pushes works
    git(github, "checkout", "-q", "feature")
    (github / "b").write_text("3")
    git(github, "commit", "-q", "-a", "-s", "--amend", "-m", "Add b, better")
    git(github, "update-ref", "refs/pull/7/head", git(github, "rev-parse", "HEAD"))
    git(github, "checkout", "-q", "main")
    again = b.push_pull_request(str(github), 7, str(gitlab), "t0ken", remote=str(github))
    assert again != head and git(gitlab, "rev-parse", "refs/heads/github/pr-7") == again


def test_merged_finds_the_pull_requests_that_landed(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a").write_text("1")
    git(tmp_path, "add", "a")
    git(tmp_path, "commit", "-q", "-m", "base")
    before = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "Merge branch 'github/pr-9' into 'main'")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "Unrelated")
    after = git(tmp_path, "rev-parse", "HEAD")
    sys.stdout = sys.__stdout__
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = b.main(["merged", "--range", f"{before}..{after}", "--repo-dir", str(tmp_path)])
    assert code == 0 and buf.getvalue().strip() == '{"merged": [9]}'
    with redirect_stdout(io.StringIO()):
        assert (
            b.main(["merged", "--range", "main..--upload-pack=x", "--repo-dir", str(tmp_path)]) == 1
        )


def commit(cwd: Path, name: str, email: str, message: str, *, signoff: str | None = None) -> None:
    body = message if signoff is None else f"{message}\n\nSigned-off-by: {signoff}"
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}  # fmt: skip
    git(cwd, "commit", "-q", "--allow-empty", "-m", body, env=env)


def test_the_dco_check_reads_real_history(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "Base", "base@example.org", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    commit(tmp_path, "Ann", "ann@example.org", "good", signoff="Ann <ann@example.org>")
    commit(tmp_path, "Bob", "bob@example.org", "forgot")
    commit(tmp_path, "Cy", "cy@example.org", "wrong person", signoff="Dee <dee@example.org>")
    commit(tmp_path, "dependabot[bot]", "bot@users.noreply.github.com", "bump")
    raw = subprocess.run(
        ["git", "-C", str(tmp_path), "log", f"--format=%H{d._SEP}%an{d._SEP}%ae{d._SEP}%ce{d._SEP}%P{d._SEP}%B{d._END}", f"{base}..HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    found = d.problems(d.parse_log(raw))
    assert len(found) == 2
    assert any("no Signed-off-by" in f for f in found) and any("does not match" in f for f in found)
    text = d.report(found)
    assert "git commit --amend --signoff" in text and "git rebase --signoff" in text


def test_merge_commits_and_case_differences_pass() -> None:
    sha = "c" * 40
    merge = d.Commit(sha, "Ann", "ann@example.org", "ann@example.org", 2, "Merge branch main")
    shouty = d.Commit(
        sha,
        "Ann",
        "Ann@Example.org",
        "ann@example.org",
        1,
        "x\n\nSigned-off-by: Ann <ANN@example.org>",
    )
    assert d.problems([merge, shouty]) == []
