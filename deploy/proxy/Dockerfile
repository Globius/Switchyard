# Serves the built UI and proxies /api, /metrics to the controller. Build from the repository root:
#   docker build -f deploy/proxy/Dockerfile -t switchyard-proxy .
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS ui
RUN corepack enable
WORKDIR /ui
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ .
RUN pnpm exec vite build

FROM nginxinc/nginx-unprivileged:stable-alpine@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
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
