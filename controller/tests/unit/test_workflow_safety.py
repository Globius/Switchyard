# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The GitHub workflows that handle pull requests from strangers stay safe (ADR 0208)."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
#: Expressions that put attacker-controlled text into a shell script (script injection).
UNTRUSTED = re.compile(
    r"\$\{\{\s*github\.event\.(pull_request\.(title|body|head\.(ref|label|repo)|user)|"
    r"issue\.(title|body)|comment\.body|head_commit\.message)[^}]*\}\}"
)


def load(name: str) -> dict[Any, Any]:
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(doc, dict)
    return doc


def steps(doc: dict[Any, Any]) -> list[tuple[str, dict[Any, Any]]]:
    return [(job, s) for job, j in doc["jobs"].items() for s in j.get("steps", [])]


@pytest.mark.parametrize("name", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_no_workflow_puts_untrusted_text_into_a_script(name: str) -> None:
    for job, step in steps(load(name)):
        assert not UNTRUSTED.search(step.get("run", "")), (
            f"{name}/{job} interpolates pull request text"
        )


def test_the_bridge_never_checks_out_or_runs_the_pull_request() -> None:
    doc = load("bridge.yml")
    assert "pull_request_target" in (doc.get("on") or doc[True])
    for job, step in steps(doc):
        uses = step.get("uses", "")
        if uses:
            assert uses.startswith("actions/checkout@"), f"{job}: unexpected action {uses}"
            with_ = step.get("with", {})
            # no ref or repository means the base branch of this repository, never the fork
            assert "ref" not in with_ and "repository" not in with_, (
                f"{job}: checkout of another ref"
            )
        for line in step.get("run", "").splitlines():
            word = line.strip().split(" ")[0] if line.strip() else ""
            assert word not in {
                "npm",
                "pnpm",
                "pip",
                "uv",
                "make",
                "docker",
                "go",
                "cargo",
                "bash",
                "sh",
            }, f"{job}: runs {word}"


def test_the_bridge_consumes_review_and_does_not_forward_synchronization_events() -> None:
    jobs = load("bridge.yml")["jobs"]
    gate = jobs["forward"]["if"]
    assert "github.event.action == 'labeled'" in gate
    assert "github.event.label.name == 'ready-for-gitlab'" in gate
    runs = [step.get("run", "") for step in jobs["forward"]["steps"]]
    consume = next(i for i, run in enumerate(runs) if "--remove-label ready-for-gitlab" in run)
    push = next(i for i, run in enumerate(runs) if "bridge.py sync" in run)
    assert consume < push
    invalidation = jobs["invalidate"]
    assert "github.event.action == 'synchronize'" in invalidation["if"]
    assert "github.event.action == 'reopened'" in invalidation["if"]
    assert "GITLAB_TOKEN" not in invalidation.get("env", {})


def test_the_gitlab_token_is_only_given_to_the_steps_that_need_it() -> None:
    for name in ("bridge.yml", "merged.yml", "dco.yml", "ci.yml", "docs.yml"):
        path = WORKFLOWS / name
        if not path.exists():
            continue
        text = path.read_text()
        if name != "bridge.yml":
            assert "GITLAB_TOKEN" not in text, name
    doc = load("bridge.yml")
    for job, j in doc["jobs"].items():
        for step in j.get("steps", []):
            assert "GITLAB_TOKEN" not in str(step.get("env", "")), f"{job}: token on a single step"


def test_pull_request_workflows_run_without_secrets() -> None:
    dco = load("dco.yml")
    triggers = dco.get("on") or dco[True]
    assert "pull_request" in triggers and "pull_request_target" not in triggers
    assert "${{ secrets" not in (WORKFLOWS / "dco.yml").read_text()


def test_external_actions_are_pinned_to_immutable_commits() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for job, step in steps(load(path.name)):
            uses = step.get("uses", "")
            if uses:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w.-]+)*@[0-9a-f]{40}", uses), (path.name, job, uses)


def test_pull_request_docs_build_cannot_publish_or_mint_identity_tokens() -> None:
    docs = load("docs.yml")
    assert docs["permissions"] == {"contents": "read"}
    assert docs["jobs"]["build"].get("permissions", docs["permissions"]) == {"contents": "read"}
    assert docs["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"


def test_vulnerability_scans_are_required_on_both_ci_hosts() -> None:
    github = load("ci.yml")["jobs"]
    gitlab = yaml.safe_load((WORKFLOWS.parents[1] / ".gitlab-ci.yml").read_text())
    for name in ("audit-python", "audit-go", "audit-ui"):
        assert not github[name].get("continue-on-error", False), name
        assert not gitlab[name].get("allow_failure", False), name
