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
