# Roll an SE group to a new template, or scale it out

A group can also scale out by itself ([automatic scale-out](../autoscale.md)). Use this runbook to put a new template (new HAProxy, FRR, Keepalived or agent version, [build-se-template.md](build-se-template.md)) under a
whole SE group without dropping the VIP, or to add engines to a group. One engine at a time, gated on the group's health score
(`GET /tenants/{t}/status/se-groups`: 80 or more for the group and for every engine).

```sh
cd deploy/lab/hosts          # the script reads hosts.env and uses lib.sh (see runbooks/lab-hosts.md)
sh upgrade.sh GROUP lxc "pve01=9761 pve02=9771"       # replace every engine; one template id per node
sh upgrade.sh --add GROUP lxc "pve01=9761" 2          # scale out: add 2 engines on pve01
```

For each engine it deploys a replacement on the same node from that node's template, enrolls it (secrets through stdin, as
`enroll-engine.sh` does), **reapplies the group** so the new engine receives the current configuration (a canary-first apply,
no revision), waits until the group scores 80 or more, retires the old engine and waits again. It stops at the first failure
and leaves both engines running, so the old one keeps serving; back out by retiring the new engine
([replace-se.md](replace-se.md)).

Watch while it runs: `GET /status/se-groups` (score and reasons), the VIP from a client, `GET /status/engines`. A new engine
shows drift until the reapply lands; that is expected, and it is why the script reapplies before it retires anything.

Order for a VRRP group: the script goes by engine name, so the master is replaced when its turn comes and the backup takes
over. A BGP group loses one ECMP path at a time.
