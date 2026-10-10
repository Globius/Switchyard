# Test-only image with the routing tools at the versions of the SE image (Debian 13):
# Keepalived 2.3 (UDP_CHECK needs 2.1 or later), ipvsadm and nftables.
FROM debian:trixie-slim
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends keepalived ipvsadm nftables iproute2 curl \
    && rm -rf /var/lib/apt/lists/*
