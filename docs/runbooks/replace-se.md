# Replace a service engine

Use when an SE is broken, runs an old template, or must move to another Proxmox host. Replace one SE at
a time so the group keeps its quorum (VRRP pair or BGP peers); check `GET /tenants/{t}/status/engines`
between each.

1. Check the group is otherwise healthy and the VIP is held by a different engine
   (`status/engines`: `reachable`, VRRP state or BGP session per engine).
2. Deploy the new engine first: `POST /tenants/{t}/se-groups/{group}/deploy` (or the UI wizard). It clones
   the template named by `template_vmid` and registers the engine.
3. Give the new engine an enrollment token: `POST /tenants/{t}/engines/{id}/enrollment-token` (shown once,
   valid one hour). Proxmox cannot upload cloud-init snippets through its API (ADR 0011), so put the token on
   the VM yourself: write `/etc/switchyard/agent/enroll.env` (`CONTROLLER_URL=…`), `enroll.token` and
   `controller-ca.crt` (see `se-image/cloud-init/se-instance.yaml.tpl`), then
   `systemctl restart sy-agent-enroll.service`.
4. Wait for the engine to show `reachable`, agent connected, and the same render fingerprint as its peers.
   The controller pushes config through the canary step; do not push by hand.
5. Retire the old engine: `POST /tenants/{t}/engines/{id}/retire` (a job: it destroys the VM and removes the
   record). If the VM is already gone, `DELETE /tenants/{t}/engines/{id}` removes only the record.
6. Check the VIP with a real request, and that the drift view shows no drift.

**Back out.** Before step 5 nothing was removed: retire the new engine instead.
