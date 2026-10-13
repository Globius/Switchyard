#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Starts Patroni. With RESTORE_TARGET_TIME set (and an empty data directory) the node bootstraps by a
# pgBackRest point-in-time restore instead of initdb; set RESTORE_TARGET_TIME="" afterwards. See
# docs/runbooks/restore-database.md.
set -eu
mkdir -p /data/pgdata && chmod 700 /data/pgdata
CONFIG=/etc/patroni.yml
# The runtime configuration is /etc/patroni.yml plus the nofailover tag and, when SY_NODE_IPS is set, the PostgreSQL TLS parameters and a pg_hba
# that admits only the controller nodes (hostssl) and this stack's Docker network; and, with RESTORE_TARGET_TIME, the
# point-in-time restore bootstrap.
# PATRONI_TAGS_NOFAILOVER=true (the ESXi member) is applied here too: Patroni has no environment variable for tags.
CONFIG=/tmp/patroni-run.yml
python - <<'PY'
import os, yaml
cfg = yaml.safe_load(open("/etc/patroni.yml"))
cfg.setdefault("tags", {})["nofailover"] = os.environ.get("PATRONI_TAGS_NOFAILOVER", "false").lower() in ("1", "true", "yes")
t = os.environ.get("RESTORE_TARGET_TIME", "")
if t:
    cfg["bootstrap"]["method"] = "pgbackrest_restore"
    cfg["bootstrap"].pop("post_bootstrap", None)  # the restored cluster already has its database and stanza
    cfg["bootstrap"]["pgbackrest_restore"] = {
        "command": f'pgbackrest --stanza=switchyard --type=time "--target={t}" --target-action=promote restore',
        "no_params": True,
        "keep_existing_recovery_conf": True,
    }
ips = [i.strip() for i in os.environ.get("SY_NODE_IPS", "").split(",") if i.strip()]
if ips:
    # Sources: the three nodes, and the Docker network of this stack (its containers reach the node address from there).
    sources = [f"{i}/32" if ":" not in i else f"{i}/128" for i in ips]
    sources.append(os.environ["SY_DOCKER_SUBNET"])
    hba = ["local all all trust"]  # the socket inside the container only
    hba += [f"hostssl replication replicator {s} scram-sha-256" for s in sources[:-1]]
    hba += [f"hostssl all all {s} scram-sha-256" for s in sources]
    hba += ["host all all 0.0.0.0/0 reject", "host all all ::/0 reject"]
    cfg["bootstrap"]["pg_hba"] = hba
    pg = cfg.setdefault("postgresql", {})
    pg["pg_hba"] = hba  # also applies to nodes that join later (replaces the DCS-wide list)
    pg["use_unix_socket"] = True  # Patroni talks to its own PostgreSQL over the socket, so loopback needs no trust entry
    pg["use_unix_socket_repl"] = True
    params = pg.setdefault("parameters", {})
    params.update({
        "ssl": "on",
        "ssl_cert_file": "/tmp/pg-server.crt",
        "ssl_key_file": "/tmp/pg-server.key",
        "ssl_ca_file": "/pki/ca.crt",
        "ssl_min_protocol_version": "TLSv1.2",
        "password_encryption": "scram-sha-256",
    })
    for src, dst in (("/pki/node.crt", "/tmp/pg-server.crt"), ("/pki/node.key", "/tmp/pg-server.key")):
        open(dst, "wb").write(open(src, "rb").read())
        os.chmod(dst, 0o600)
    cfg["bootstrap"]["dcs"]["postgresql"]["parameters"].update(params)
yaml.safe_dump(cfg, open("/tmp/patroni-run.yml", "w"))
PY
exec patroni "$CONFIG"
