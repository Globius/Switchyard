# Switchyard controller: one image, two entrypoints (`syctl serve`, `syctl worker`).
# Build from the repository root: docker build -f deploy/controller/Dockerfile -t switchyard-controller .
FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8 AS build
# uv from PyPI, not from ghcr.io: ghcr.io has no IPv6, so an IPv6-only build host could not fetch it.
RUN pip install --no-cache-dir "uv==0.12.23"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY controller/pyproject.toml controller/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY controller/switchyard ./switchyard
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
RUN addgroup -S -g 10001 switchyard \
 && adduser -S -D -H -h /nonexistent -s /sbin/nologin -u 10001 -G switchyard switchyard
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
# Dependencies are prepared in the build stage; the service needs no package installer.
# Remove inherited pip and its ensurepip wheel rather than ship its installation advisories.
RUN apk upgrade --no-cache zlib \
 && /usr/local/bin/python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.12/ensurepip \
 && mkdir -p /var/lib/switchyard/staging /var/lib/switchyard/backups \
 && chown -R 10001 /var/lib/switchyard
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=2)"]
ENTRYPOINT ["syctl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
