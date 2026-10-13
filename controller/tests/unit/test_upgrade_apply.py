# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Applying a bundle to a Compose node: order of steps, revert on failure (ADR 0205)."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.domain.bundle import BundleError
from switchyard.domain.releases import public_key_b64
from switchyard.services.release_bundle import Input, build
from switchyard.services.upgrade_apply import ApplyError, apply_compose, read_env, set_env

KEY = Ed25519PrivateKey.generate()
TRUSTED = {"k1": public_key_b64(KEY)}


class Fake:
    """Records commands; answers `docker load` with an image name; can fail chosen steps."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:2] == ["docker", "load"]:
            name = "controller" if "controller" in argv[-1] else "proxy"
            return 0, f"Loaded image: switchyard-{name}:0.2.0\n"
        verb = next((v for v in ("run", "up") if v in argv), "")
        return (1, "boom") if verb in self.fail else (0, "")

    def verbs(self) -> list[str]:
        return [next((a for a in c if a in ("load", "run", "up")), "?") for c in self.calls]


@pytest.fixture
def node(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "controller.tar").write_bytes(b"c")
    (src / "proxy.tar").write_bytes(b"p")
    bundle = tmp_path / "b.sybundle"
    build(
        bundle,
        version="0.2.0",
        schema_head="0046",
        created_at="2026-10-13T04:00:00Z",
        inputs=[
            Input("controller-image", src / "controller.tar", "controller.tar"),
            Input("proxy-image", src / "proxy.tar", "proxy.tar"),
        ],
        key=KEY,
        key_id="k1",
    )
    env = tmp_path / "node.env"
    env.write_text("NODE_NAME=ctl-1\nCONTROLLER_IMAGE=switchyard-controller:0.1.0\n# keep\n")
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    return {"bundle": bundle, "env": env, "compose": tmp_path / "compose.yaml", "root": tmp_path}


async def go(
    node: dict[str, Path], fake: Fake, healthy: bool = True, trusted: dict[str, str] = TRUSTED
) -> None:
    async def ok() -> bool:
        return healthy

    await apply_compose(
        node["bundle"], trusted, compose_file=node["compose"], env_file=node["env"],
        workdir=node["root"] / "work", healthy=ok, run=fake, health_attempts=2, health_delay_s=0,
    )  # fmt: skip


async def test_a_healthy_upgrade_runs_the_steps_in_order(node: dict[str, Path]) -> None:
    fake = Fake()
    await go(node, fake)
    assert fake.verbs() == ["load", "load", "run", "up"]
    env = read_env(node["env"].read_text())
    assert env["CONTROLLER_IMAGE"] == "switchyard-controller:0.2.0"
    assert env["PROXY_IMAGE"] == "switchyard-proxy:0.2.0" and env["NODE_NAME"] == "ctl-1"
    assert "# keep" in node["env"].read_text()
    assert all("sh" not in c[0] for c in fake.calls)  # argument lists, no shell


async def test_an_unhealthy_node_goes_back_to_the_old_images(node: dict[str, Path]) -> None:
    before = node["env"].read_text()
    fake = Fake()
    with pytest.raises(ApplyError, match="did not become healthy") as err:
        await go(node, fake, healthy=False)
    assert err.value.rolled_back is False  # still unhealthy after the revert
    assert node["env"].read_text() == before
    assert fake.verbs() == ["load", "load", "run", "up", "up"]


async def test_a_failed_migration_leaves_the_node_on_the_old_images(node: dict[str, Path]) -> None:
    before = node["env"].read_text()
    fake = Fake(fail={"run"})
    with pytest.raises(ApplyError, match="migration failed"):
        await go(node, fake)
    assert node["env"].read_text() == before
    assert "up" not in fake.verbs()


async def test_a_bad_bundle_touches_nothing(node: dict[str, Path]) -> None:
    fake = Fake()
    before = node["env"].read_text()
    with pytest.raises(BundleError):
        await go(node, fake, trusted={})
    assert fake.calls == [] and node["env"].read_text() == before


def test_env_editing_keeps_other_lines() -> None:
    assert set_env("A=1\n# note\nB=2\n", {"B": "9", "C": "3"}) == "A=1\n# note\nB=9\nC=3\n"


async def test_an_override_file_is_passed_to_every_compose_call(node: dict[str, Path]) -> None:
    fake = Fake()
    override = node["root"] / "compose.override.yaml"
    override.write_text("services: {}\n")

    async def ok() -> bool:
        return True

    await apply_compose(
        node["bundle"], TRUSTED, compose_file=[node["compose"], override], env_file=node["env"],
        workdir=node["root"] / "work", healthy=ok, run=fake, health_attempts=1, health_delay_s=0,
    )  # fmt: skip
    compose_calls = [c for c in fake.calls if c[:2] == ["docker", "compose"]]
    assert compose_calls
    for call in compose_calls:
        assert call.count("-f") == 2 and str(override) in call
