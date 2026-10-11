# Serves the built UI and proxies /api, /metrics to the controller. Build from the repository root:
#   docker build -f deploy/proxy/Dockerfile -t switchyard-proxy .
FROM node:22-slim AS ui
RUN corepack enable
WORKDIR /ui
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ .
RUN pnpm exec vite build

FROM nginx:1.27-alpine
COPY --from=ui /ui/dist /usr/share/nginx/html
COPY deploy/proxy/nginx.conf /etc/nginx/templates/default.conf.template
COPY deploy/proxy/sy-proxy.conf /etc/nginx/sy-proxy.conf
ENV CONTROLLER_UPSTREAM=api:8000
EXPOSE 8080
