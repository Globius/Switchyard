# 0144. What the SE image and the agent must provide for the WAF

Status: Accepted
Date: 2026-10-12

## Decisions

1. **SE image:** `coraza-spoa` (a pinned release) at `/usr/local/bin/coraza-spoa`, the OWASP CRS releases listed in `CRS_VERSIONS`, each in its own
   directory `/usr/share/coreruleset/<version>/` (the generated directives include `crs-setup.conf.example` and `rules/*.conf` from the pinned
   version's directory, never a `current` link, so a policy cannot drift), a `coraza` user, the unit `se-image/files/coraza-spoa.service` (enabled, hardened, read-only access to
   `/etc/switchyard/coraza`), and the Vector source added to `se-image/files/vector.yaml`. `provision.sh` and `fetch-binaries.sh` belong to the
   Operations thread (P7); this change adds the unit and the Vector source and asks P7 to wire the binary and the CRS in.
2. **Agent:** no change. The `coraza` directory and the `coraza-spoa` reload are already in the allowlist; the agent still writes only under
   `/etc/switchyard/` and starts no other process.
3. **Controller:** `render/waf.py` produces both files; `pipeline/bundles.py` adds `coraza/config.yaml` to the bundle of every engine of a group
   that has a WAF policy. A group without one gets no `coraza` files.
4. An engine built from an image without the sidecar fails the reload of `coraza-spoa`, the agent restores the previous files, and the apply
   stops at the canary. That is the intended guard against attaching a WAF policy to a group that was deployed from an old image.
