# PostgreSQL 17 + Patroni + pgBackRest. Node settings come from PATRONI_* and PGBACKREST_* variables.
FROM postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends python3-pip python3-venv pgbackrest curl \
 && python3 -m venv /opt/patroni \
 && /opt/patroni/bin/pip install --no-cache-dir "patroni[etcd3]==4.0.*" psycopg2-binary \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest /etc/pgbackrest /data \
 && chown -R postgres:postgres /var/lib/pgbackrest /var/log/pgbackrest /var/spool/pgbackrest /etc/pgbackrest /data
COPY patroni.yml /etc/patroni.yml
COPY entrypoint.sh post_bootstrap.sh /
USER root
RUN chmod +x /entrypoint.sh /post_bootstrap.sh
USER postgres
ENV PATH=/opt/patroni/bin:$PATH
ENTRYPOINT ["/entrypoint.sh"]
