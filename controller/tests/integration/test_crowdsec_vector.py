# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The CrowdSec log feed runs in real Vector: the drop-in merges with the image's config and wraps lines (ADR 0215).

The engine's ``vector.yaml`` (``se-image/files/vector.yaml``) and the drop-in the controller renders are
loaded together exactly as the unit does (``--config`` plus ``--config-dir``). ``vector test`` then feeds
events to the drop-in's first transform and checks what leaves the second.
"""

import io
import tarfile
import time
from pathlib import Path

import docker
import pytest

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.render.crowdsec import LOG_FEED_PATH, BouncerSettings, render_log_feed_files
from tests.factories import BASE

VECTOR_IMAGE = "timberio/vector:0.46.1-debian"
IMAGE_CONFIG = Path(__file__).parents[3] / "se-image" / "files" / "vector.yaml"
LINE = (
    "192.0.2.99:41234 [16/Oct/2026:16:00:00.123] vs_web be_pool/member1 0/0/1/2/3 404 150 - - ---- "
    '1/1/0/0/0 0/0 "GET /probe-1 HTTP/1.1"'
)

TESTS = f"""
tests:
  - name: a shared service is wrapped as syslog
    inputs:
      - insert_at: crowdsec_select
        type: log
        log_fields:
          frontend: vs_web
          host: engine-1
          message: '{LINE}'
    outputs:
      - extract_from: crowdsec_syslog
        conditions:
          - type: vrl
            source: |
              m = string!(.message)
              assert!(starts_with(m, "<134>"), "priority")
              assert!(contains(m, " engine-1 haproxy[1]: 192.0.2.99:41234 "), "host, program and line")
              assert!(ends_with(m, "GET /probe-1 HTTP/1.1\\""), "the line is intact")
              true
  - name: another service is not selected
    inputs:
      - insert_at: crowdsec_select
        type: log
        log_fields:
          frontend: vs_other
          host: engine-1
          message: '{LINE}'
    no_outputs_from:
      - crowdsec_select
"""


def feed_text() -> str:
    d = {**BASE}
    d["policy_sets"] = [
        {"id": "ps-cs", "tenant_id": "t-acme", "name": "cs", "kind": "crowdsec", "share_logs": True}
    ]
    d["virtual_services"] = [dict(d["virtual_services"][0], policy_set_ids=["ps-cs"])]
    snap = snapshot_from_mapping(d)
    files = render_log_feed_files(
        snap,
        snap.se_groups[0].id,
        BouncerSettings("https://x.example.test", "k", (), "crowdsec.example.test:514"),
    )
    return files[LOG_FEED_PATH]


def tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def run_vector(args: list[str], files: dict[str, str]) -> tuple[int, str]:
    client = docker.from_env()
    container = client.containers.create(
        VECTOR_IMAGE,
        entrypoint=["vector"],
        command=args,
        environment={
            "LOKI_URL": "http://loki.invalid:3100",
            "TENANT_ID": "t",
            "TENANT_NAME": "acme",
            "SE_GROUP": "edge",
            "ENGINE": "engine-1",
        },
    )
    try:
        container.put_archive("/", tar(files))
        container.start()
        deadline = time.monotonic() + 120
        while container.reload() or container.status == "running":
            if time.monotonic() > deadline:
                raise TimeoutError("vector did not finish")
            time.sleep(0.5)
        code = int(container.attrs["State"]["ExitCode"])
        return code, container.logs().decode(errors="replace")
    finally:
        container.remove(force=True)


def files() -> dict[str, str]:
    return {
        "cfg/vector.yaml": IMAGE_CONFIG.read_text("utf-8"),
        "cfg/vector/crowdsec.yaml": feed_text(),
        "cfg/tests.yaml": TESTS,
    }


@pytest.mark.parametrize("with_dropin", [True, False])
def test_vector_loads_the_image_config_with_and_without_the_dropin(with_dropin: bool) -> None:
    f = files()
    if not with_dropin:
        del f["cfg/vector/crowdsec.yaml"]
    del f["cfg/tests.yaml"]
    f.setdefault("cfg/vector/.keep", "")  # an empty directory must load too
    code, output = run_vector(
        ["validate", "--no-environment", "--config-dir", "/cfg/vector", "/cfg/vector.yaml"], f
    )
    assert code == 0, output


def test_the_dropin_selects_by_frontend_and_wraps_lines_for_the_syslog_datasource() -> None:
    code, output = run_vector(
        ["test", "/cfg/vector.yaml", "/cfg/vector/crowdsec.yaml", "/cfg/tests.yaml"], files()
    )
    assert code == 0, output


def test_the_feed_text_is_the_documented_shape() -> None:
    text = feed_text()
    assert text.startswith("# Managed by Switchyard")
    assert 'address: "crowdsec.example.test:514"' in text
    assert isinstance(text, str) and text.endswith("\n")
