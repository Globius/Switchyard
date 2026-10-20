# Serves the built UI and proxies /api, /metrics to the controller. Build from the repository root:
#   docker build -f deploy/proxy/Dockerfile -t switchyard-proxy .
FROM node:26-slim@sha256:0d1e33586362bb9e608c4e751e7a9e7edb55c9ac11461ed5666af696cc461dd4 AS ui
RUN corepack enable
WORKDIR /ui
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ .
RUN pnpm exec vite build

FROM nginxinc/nginx-unprivileged:stable-alpine@sha256:56b8272e43a0525f67aed68d1e0794a712d10e3e45a563e118e9a77c817e6f86
USER root
# Upgrade the pinned base's zlib to the signed Alpine security update (CVE-2026-85091).
RUN apk upgrade --no-cache zlib
COPY --chown=101:101 --from=ui /ui/dist /usr/share/nginx/html
COPY --chmod=0644 deploy/proxy/nginx.conf /etc/nginx/templates/default.conf.template
COPY --chmod=0644 deploy/proxy/sy-proxy.conf /etc/nginx/sy-proxy.conf
COPY --chmod=0755 deploy/proxy/15-trusted-proxies.sh /docker-entrypoint.d/15-trusted-proxies.sh
RUN chmod 755 /etc/nginx/templates \
 && find /usr/share/nginx/html -type d -exec chmod 755 {} + \
 && find /usr/share/nginx/html -type f -exec chmod 644 {} +
ENV CONTROLLER_UPSTREAM=api:8000 NGINX_RESOLVER=127.0.0.11 TRUSTED_PROXY_CIDRS=""
USER 101
EXPOSE 8080
