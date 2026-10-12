# Automatic scale-out of an SE group

A busy SE group can add engines by itself. It only ever adds: taking an engine away stays a deliberate
[retire](runbooks/replace-se.md). The steps are the manual ones of [Roll an SE group to a new template, or scale it out](runbooks/rolling-upgrade.md),
done by the controller.

**Needs:** Proxmox configured on the controller (`SWITCHYARD_PVE_URL` and a token), agent enrollment (OpenBao PKI and the bundle
key), an SE template with the guest agent, and QEMU engines (a container has no guest agent API, so it cannot be enrolled this way).
Not lab-tested yet: see [ADR 0194](adr/0194-autoscale-and-packet-capture.md).

## Turn it on

Add an `autoscale` block to the SE group. It is desired state like the rest, so it goes through a change set.

```yaml
se_groups:
  - name: edge
    # ... networks, ha_mode ...
    autoscale:
      enabled: true
      max_engines: 6          # never more than this (2 to 16)
      scale_out_at: 70        # percent of each engine's connection limit that counts as busy
      sustain_s: 300          # busy for this long before an engine is added
      cooldown_s: 900         # minimum time between two additions
      deploy:                 # what to clone, as in a manual deploy
        nodes: [pve1, pve2]
        template_vmid: 9000
        mgmt_pool_id: <VIP pool on the group's mgmt network>
        credentials_ref: kv/tenants/acme/se/dataplane      # "user:password" of the Data Plane API
        nic_pools: {}         # network id -> pool for the data NICs, if they take addresses
      enroll:
        controller_url: https://switchyard.example.test:8443
        controller_cn: switchyard-controller
        ca_ref: kv/tenants/acme/controller-ca              # the controller CA certificate (PEM)
```

The group needs exactly one `mgmt` network, every network needs a VNet, and the pools must belong to those networks; the
validator says which path is wrong (`autoscale_pool_mismatch`, `autoscale_needs_mgmt_network`, `autoscale_network_without_vnet`).

## What it does

Every 30 seconds, per group:

1. Averages the engines' open connections as a percent of each engine's own limit (from the Data Plane API, kept with the
   engine status).
2. After `sustain_s` at or above `scale_out_at`, and only if the group is under `max_engines`, outside its cooldown, scoring 80
   or more on the [health score](features/engines.md), and not already adding an engine, it queues a deploy for one more engine.
   The new engine is registered **disabled**, so rollouts and polls leave it alone while it boots.
3. When the VM exists, it creates the engine's one-time enrollment token and writes the enrollment files into the guest through
   the Proxmox guest agent. It retries every tick until the guest agent answers.
4. When the engine's agent answers, it enables the engine and reapplies the group (canary first, no revision). The cooldown starts.

## See what it is doing

`GET /api/v1/tenants/{t}/autoscale` lists each autoscaled group: engines and limit, the measured `utilization`, and the
`decision`:

| Decision | Meaning |
| --- | --- |
| `idle` | Below the threshold, or no load data yet |
| `busy` | At or above the threshold; waiting out `sustain_s` |
| `scaling` | An engine is deploying or enrolling |
| `cooldown` | An engine joined recently |
| `at_max` | Busy, and the group is at `max_engines` |
| `blocked` | Stopped; `reason` says why: the group is unhealthy, the deploy failed, or the new engine has not enrolled after 20 minutes |
| `off` | `enabled: false` |

A `blocked` group adds nothing until someone retires the engine that never enrolled (or fixes the template) and the cause is
gone. Every scale-out and join is in the [audit log](features/audit.md) (`autoscale.scale_out`, `autoscale.engine_joined`).
