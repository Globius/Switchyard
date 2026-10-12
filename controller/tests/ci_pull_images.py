"""Pull the container images the Docker-backed tests start, before the tests run.

Several tests create containers with the Docker SDK, which does not pull a missing image. On a
developer machine the images are already there; a CI job starts with an empty Docker daemon. Run with
``uv run python -m tests.ci_pull_images`` from ``controller/``.

Example:
    $ uv run python -m tests.ci_pull_images
    pulled haproxy:3.2
"""

from __future__ import annotations

import docker

#: Every image a test starts by name. Keep in step with the tests (grep for ``containers.create``).
IMAGES = (
    "alpine:3.20",
    "busybox:1.36",
    "curlimages/curl:8.10.1",
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


def main() -> None:
    """Pull each image in ``IMAGES``."""
    client = docker.from_env()
    for image in IMAGES:
        repo, tag = image.rsplit(":", 1)
        client.images.pull(repo, tag=tag)
        print(f"pulled {image}")  # noqa: T201 - progress in the CI log


if __name__ == "__main__":
    main()
