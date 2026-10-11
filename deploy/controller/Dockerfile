# Switchyard controller: one image, two entrypoints (`syctl serve`, `syctl worker`).
# Build from the repository root: docker build -f deploy/controller/Dockerfile -t switchyard-controller .
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY controller/pyproject.toml controller/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY controller/switchyard ./switchyard
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin switchyard
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=2)"]
ENTRYPOINT ["syctl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
