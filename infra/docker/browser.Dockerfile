# syntax=docker/dockerfile:1
# worker-browser image (architecture.md §10; K84): Microsoft's Playwright Python image
# (Chromium and its system libraries) with the backend package and the `browser` dependency group
# (playwright). It runs `python -m kalkan.workers browser`. Build context: repository root.
#
# The Python is the image's own (Ubuntu 24.04's 3.12), not the 3.12.15 of the backend image (K75):
# the browser worker drives Chromium and leaves HTML parsing to worker-default. The image and the
# locked playwright package must have the same version (v1.63.0); otherwise Playwright cannot find
# the browsers under /ms-playwright.

FROM ghcr.io/astral-sh/uv:0.12.24 AS uv

FROM mcr.microsoft.com/playwright/python:v1.64.1-noble AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --group browser --no-install-project
COPY backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --group browser --no-editable

FROM mcr.microsoft.com/playwright/python:v1.64.1-noble AS runtime
# Non-root pwuser (the image's user) with the uid and gid of the backend image's kalkan user, so
# worker-browser and worker-default share files on the artifacts-tmp volume, whose mount point
# both images own (a fresh named volume takes its owner from the image).
# The base image leaves /ms-playwright world-writable; a compromised browser must not be able to
# replace the binaries that later jobs launch.
RUN groupmod --gid 10001 pwuser \
    && usermod --uid 10001 --gid 10001 pwuser \
    && chown -R pwuser:pwuser /home/pwuser \
    && install -d -o 10001 -g 10001 -m 0770 /artifacts-tmp \
    && chmod -R go-w /ms-playwright
COPY --from=builder --chown=root:root /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The playwright package must find this image's browsers: a version mismatch fails the build.
RUN python -c "import kalkan.config; from pathlib import Path; \
from playwright.sync_api import sync_playwright; \
p = sync_playwright().start(); assert Path(p.chromium.executable_path).exists(); p.stop()"
ARG GIT_SHA=dev
ENV GIT_SHA=$GIT_SHA
WORKDIR /app
USER pwuser
# Chromium runs with its sandbox (K6); compose drops every capability and sets a seccomp profile.
CMD ["python", "-m", "kalkan.workers", "browser"]
