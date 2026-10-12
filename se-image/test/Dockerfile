# Runs provision.sh on Debian 13 and checks the result. Build from the repository root:
#   docker build -f se-image/test/Dockerfile -t sy-se-image-test .
FROM golang:1.26 AS agent
WORKDIR /src
COPY agent/ .
RUN CGO_ENABLED=0 go build -o /sy-agent ./cmd/sy-agent

FROM haproxytech/haproxy-debian:3.2 AS dp

FROM debian:trixie AS auth
COPY se-image/versions.env /versions.env
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates  && . /versions.env && curl -fsSL -o /o.tgz "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/$OAUTH2_PROXY_VERSION/oauth2-proxy-$OAUTH2_PROXY_VERSION.linux-amd64.tar.gz"  && echo "$OAUTH2_PROXY_SHA256_LINUX_AMD64  /o.tgz" | sha256sum -c -  && tar -xzf /o.tgz --strip-components=1 "oauth2-proxy-$OAUTH2_PROXY_VERSION.linux-amd64/oauth2-proxy" -C /

FROM debian:trixie AS provisioned
COPY --from=agent /sy-agent /tmp/sy-agent
COPY --from=auth /oauth2-proxy /tmp/oauth2-proxy
COPY --from=dp /usr/local/bin/dataplaneapi /tmp/dataplaneapi
COPY se-image/ /opt/se-image/
RUN AGENT_BIN=/tmp/sy-agent DATAPLANE_BIN=/tmp/dataplaneapi AUTH_PROXY_BIN=/tmp/oauth2-proxy /opt/se-image/provision.sh
FROM provisioned
RUN sh /opt/se-image/test/check.sh
