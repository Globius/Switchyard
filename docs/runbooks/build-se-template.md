# Build or refresh the SE template

When: first install, or to roll new HAProxy, FRR, Keepalived or `sy-agent` versions into new SEs.
Existing SEs are not touched; replace them one at a time (`replace-se.md`).

1. Test the recipe: `docker build -f se-image/test/Dockerfile -t sy-se-image-test .` (must end with
   "se-image checks passed" and "dataplaneapi mTLS ok").
2. Build the agent: `cd agent && CGO_ENABLED=0 go build -o sy-agent ./cmd/sy-agent`.
3. Create the build VM: in `se-image/tofu`, set `endpoint`, `api_token`, `node`, `user_data_file`
   (`cloud-init/template-build.yaml`), then `tofu init && tofu apply`.
4. Copy `se-image/` and `sy-agent` to `/opt/switchyard-build/` on the VM (qemu guest agent or scp from your workstation),
   and reboot it; cloud-init runs `provision.sh` and powers the VM off.
5. In Proxmox: remove the cloud-init drive contents (`qm set <vmid> --delete ciuser`), then `qm template <vmid>`.
6. Set the vmid as `template_vmid` on the SE group (or the Proxmox settings) and deploy one SE as a canary.
7. Check on the new SE: `sy-agent`, `dataplaneapi`, `prometheus-node-exporter` active; it shows up in the group.

**LXC instead of a VM.** On the Proxmox node run `se-image/lxc/build-template.sh <vmid> <sy-agent> <dataplaneapi>`
(copy `se-image/` and the two binaries to the node first). It creates a privileged Debian 13 container with nesting,
runs `provision.sh` inside, and converts it to a template. Deploy requests use it with `guest_kind: "lxc"`. The host
needs `ip_vs`, `ip_vs_rr`, `ip_vs_wrr`, `ip_vs_sh` and `nf_conntrack` loaded; containers have no cloud-init, so push the
enrollment files in with `pct push` (see `replace-se.md`).

Rollback: keep the previous template vmid; point `template_vmid` back.
