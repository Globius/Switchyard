# 0214. Pilot deployment fixes: template node, new-engine apply, pinned build images

Status: Accepted
Date: 2026-10-16

## Context

The first pilot of the Kubernetes shape (GitHub issue 9) hit three behaviours that need a recorded decision.

- A Proxmox VMID is unique across the cluster and a template lives on one node. The adapter asked the *target* node for the template, so a
  deploy to any other node failed even when the template disk was on shared storage.
- Engines that enroll after a group's first apply received no configuration until an operator ran a reapply.
- The two Dockerfiles did not build as committed, because base images drifted from what the build needs.

## Decision

- `create_vm` finds the template's node with `GET /cluster/resources?type=vm`, reads and clones from that node, and sets `target` to the
  engine's node. If the clone or the configuration fails, the half-configured clone is destroyed (best effort) before the error is raised.
- `sshkeys` is sent percent-encoded (`quote(keys, safe="")`), as the Proxmox API requires for that one option.
- Enrollment does **not** trigger an apply by itself. Every apply goes through the canary step and an operator decides when a new engine
  joins; autoscale and rolls already reapply explicitly. The reapply step is documented in `docs/deploy-kubernetes.md`.
- Base images are pinned by digest: `python:3.12-alpine` (the version `uv.lock` requires) and `node:22-slim` (Node 26 has no corepack).
  CI builds both images on every change to their inputs, so a drift fails the pipeline.

## Consequences

Deploys can spread engines over several nodes with a template on shared storage. A failed deploy leaves no stray guest. A new engine stays
idle until a reapply, which is the existing, explicit behaviour. Digests need a periodic bump, as the other pinned images do.
