# 0118. Persistence table sync between engines (peers)

Status: Accepted
Date: 2026-10-12

## Context

Plan item C3. Each engine keeps its own stick tables, so after a VIP moves to another engine every client is re-balanced and loses its session.
HAProxy's `peers` section replicates the tables between engines.

## Decisions

1. `SeGroup.peers` (`port`, default 10000, and `members`, 2 to 16 entries of `name` and `address`) is optional desired state of the group.
   Without it every table stays local, as before.
2. The renderer writes one `peers sy_peers` section, members sorted by name so the output is stable, and adds `peers sy_peers` to every stick
   table (source IP, header and URI persistence). Cookie persistence has no table and needs no sync, because the client carries the choice.
3. HAProxy takes the local peer from the engine's host name, so each `name` must equal the host name of that engine. The deploy job names the VM
   and sets the host name from the same value, so a deployed group fills this in from its engines. If a name does not match, HAProxy drops the
   section and the engine falls back to local tables; it keeps serving.
4. Each `address` must sit in a backend-role network of the group (`peer_outside_backend_network`). The peer port must be open between the
   engines on that network; the SE image's firewall allows the group's backend network.
5. The engine list is set through the API or by the deploy job rather than derived at render time, because the renderer is pure and the same
   bytes go to every engine.
6. Table: `se_group.peers` (JSON, migration 0030). The sandbox `haproxy -c` runs with the first peer's name as its host name.
