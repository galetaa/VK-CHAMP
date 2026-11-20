FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -r /var/lib/apt/lists
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
COPY pyproject.toml .python-version uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY services ./services
COPY migrations ./migrations
COPY packages ./packages
COPY fixtures ./fixtures
COPY alembic.ini ./
RUN uv sync --locked --no-dev && mkdir -p /data/media && chown -R 10001:10001 /data
USER 10001:10001
ENV PATH=/app/.venv/bin:$PATH
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
