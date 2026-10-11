# Runs provision.sh on Debian 13 and checks the result. Build from the repository root:
#   docker build -f se-image/test/Dockerfile -t sy-se-image-test .
FROM golang:1.26 AS agent
WORKDIR /src
COPY agent/ .
RUN CGO_ENABLED=0 go build -o /sy-agent ./cmd/sy-agent

FROM haproxytech/haproxy-debian:3.2 AS dp

FROM debian:trixie AS provisioned
COPY --from=agent /sy-agent /tmp/sy-agent
COPY --from=dp /usr/local/bin/dataplaneapi /tmp/dataplaneapi
COPY se-image/ /opt/se-image/
RUN AGENT_BIN=/tmp/sy-agent DATAPLANE_BIN=/tmp/dataplaneapi /opt/se-image/provision.sh
FROM provisioned
RUN sh /opt/se-image/test/check.sh
