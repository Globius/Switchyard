# 0145. Pool discovery by Proxmox tag

Status: Accepted
Date: 2026-10-12

## Context

Avi, NGINX and F5 fill a pool from the platform (VM tags, cloud APIs). Here members are typed in. Plan item C9 (08, 09 block P12)
asks for members that follow Proxmox guests.

## Decisions

1. `Pool.discovery` (`source: proxmox_tag`, `tag`, `cidr`, `port`, `weight`) turns a pool into a discovered one. The `pool` table gets
   a `discovery` JSON column (migration 0025). Nothing in the renderer changes: discovered members are ordinary members.
2. A job, `discover_pool_members` (every minute, only where a Proxmox client is configured), lists guests with
   `GET /cluster/resources?type=vm`, asks each running tagged guest for its addresses (QEMU guest agent
   `network-get-interfaces`, or `lxc/<id>/interfaces`) and takes the first address inside `cidr`. Templates and stopped guests do not
   count.
3. The result goes through a normal change set opened by `system:discovery` and applied like any other: canary, revision, Git
   mirror, approval when the tenant requires it (then it waits, as certificate renewals do). No path writes members around the pipeline.
4. **Tenancy.** Proxmox tags are cluster-wide, so a tag must begin with `sy-<tenant>.` (`discovery_tag_not_allowed`, checked on every
   validation). Tenant names never contain `.`, so `sy-acme.` is not a prefix of the tags of tenant `acme-x`. This is a convention
   the Proxmox administrator applies when tagging guests; the controller does not tag guests.
5. Member ids are `uuid5(namespace, "<pool id>:<vmid>")` and names `pve-<vmid>`, so they are stable and a guest that moves node or
   changes address is the same member. Weight and admin state set by an operator on a discovered member are kept; a drain survives.
6. Safety: a guest whose addresses cannot be read (no agent yet) keeps its current member and is not added; a run that would leave a
   pool with no members is skipped and logged (an unreachable or empty Proxmox answer must not take a service down); at most one
   discovery change set per tenant is open at a time. Members added by hand to a discovered pool are replaced at the next change.
7. Needs the Proxmox token to read the cluster (`VM.Audit` and `VM.Monitor` for the agent call). The role the controller already
   holds for deployment is enough in the lab; production should use a read-only role for it.

## Consequences

- The UI needs a discovery block on the pool form and a "managed by discovery" note on the member list (listed in the MR).
- Only Proxmox exists as a source. Another source (Kubernetes, D2) adds a `source` value and its own ADR.
- Health checks still decide traffic. Discovery adds and removes members; it never marks anything up.
