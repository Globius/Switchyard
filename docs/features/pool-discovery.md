# Pool discovery from Proxmox tags

**What.** A pool with `discovery` is filled from running Proxmox guests that carry a tag, using the first address the guest
reports inside a network.

**How to use.**

```yaml
discovery: {source: proxmox_tag, tag: sy-acme.web, cidr: 10.0.1.0/24, port: 8080, weight: 100}
```

The `discover_pool_members` job runs every minute and opens a normal change set when members change. The full guide:
[Pool discovery](../pool-discovery.md).

**Limits.** Tags must start with `sy-<tenant>.`. Guests without a guest agent (VMs) keep their member and are not added. A run
that would empty a pool is skipped. Members added by hand to a discovered pool are replaced.

**Decision:** [0145](../adr/0145-pool-discovery-by-proxmox-tag.md).
