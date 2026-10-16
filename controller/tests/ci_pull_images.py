# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pull the container images the Docker-backed tests start, before the tests run.

Several tests create containers with the Docker SDK, which does not pull a missing image. On a
developer machine the images are already there; a CI job starts with an empty Docker daemon. Run with
``uv run python -m tests.ci_pull_images`` from ``controller/``.

In GitLab CI, Docker Hub images are pulled through the group's Dependency Proxy (a cache, so pipelines do not
hit Docker Hub's rate limit) and tagged with their usual names, so the tests find them unchanged. Images on
other registries (``ghcr.io``, ``quay.io``) are pulled directly.

Example:
    $ uv run python -m tests.ci_pull_images
    pulled haproxy:3.2
"""

from __future__ import annotations

import os

import docker

#: Every image a test starts by name. Keep in step with the tests (grep for ``containers.create``).
IMAGES = (
    "alpine:3.20",
    "busybox:1.36",
    "curlimages/curl:8.10.1",
    "crowdsecurity/crowdsec:v1.7.0",
    "ghcr.io/dexidp/dex:v2.41.1-alpine",
    "ghcr.io/letsencrypt/pebble:latest",
    "grafana/grafana:11.4.0",
    "grafana/loki:3.3.2",
    "haproxy:3.2",
    "haproxytech/haproxy-debian:3.2",
    "postgres:17",
    "powerdns/dnsdist-19:latest",
    "powerdns/pdns-auth-50:latest",
    "prom/prometheus:v3.0.1",
    "quay.io/frrouting/frr:10.2.1",
)


def _on_docker_hub(image: str) -> bool:
    """True when ``image`` has no registry host, so it comes from Docker Hub."""
    first = image.split("/", 1)[0]
    return "/" not in image or not ("." in first or ":" in first or first == "localhost")


def main() -> None:
    """Pull each image in ``IMAGES``, through the Dependency Proxy when CI provides one."""
    client = docker.from_env()
    proxy = os.environ.get("CI_DEPENDENCY_PROXY_GROUP_IMAGE_PREFIX")
    auth = None
    if proxy:
        auth = {
            "username": os.environ["CI_DEPENDENCY_PROXY_USER"],
            "password": os.environ["CI_DEPENDENCY_PROXY_PASSWORD"],
        }
    for image in IMAGES:
        source = f"{proxy}/{image}" if proxy and _on_docker_hub(image) else image
        repo, tag = source.rsplit(":", 1)
        pulled = client.images.pull(repo, tag=tag, auth_config=auth if source != image else None)
        if source != image:
            name, name_tag = image.rsplit(":", 1)
            pulled.tag(name, tag=name_tag)
        print(f"pulled {image} from {source}")  # noqa: T201 - progress in the CI log


if __name__ == "__main__":
    main()
