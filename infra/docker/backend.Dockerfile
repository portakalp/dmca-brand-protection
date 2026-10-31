# syntax=docker/dockerfile:1
# One image for api, worker-default, worker-ai, scheduler, ct-consumer and ops; each service
# runs it with a different command (architecture.md §3). Build context: repository root.
# Without the dev and browser dependency groups: Playwright lives in the browser image only.

FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM python:3.12.15-slim-trixie AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project
COPY backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-editable

FROM python:3.12.15-slim-trixie AS runtime
# /artifacts-tmp: the mount point of the artifacts-tmp volume shared with worker-browser (§10).
# /data/objects: STORAGE_ROOT of STORAGE_BACKEND=filesystem (deploy/selfhost); a fresh named
# volume takes this owner, so every service of this image can write the objects it shares.
RUN groupadd --system --gid 10001 kalkan \
    && useradd --system --uid 10001 --gid kalkan --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin kalkan \
    && install -d -o 10001 -g 10001 -m 0770 /artifacts-tmp \
    && install -d -o 10001 -g 10001 -m 0750 /data/objects
COPY --from=builder --chown=root:root /app/.venv /app/.venv
# Migrations run from this image as a one-off container (kalkan_owner).
COPY --chown=root:root backend/alembic.ini /app/alembic.ini
COPY --chown=root:root backend/alembic /app/alembic
ARG GIT_SHA=dev
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_SHA=$GIT_SHA
WORKDIR /app
USER kalkan
EXPOSE 8000
CMD ["uvicorn", "kalkan.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
