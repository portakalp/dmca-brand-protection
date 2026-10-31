# syntax=docker/dockerfile:1
# The customer panel (architecture.md §18): Next.js standalone output.
# Build context: repository root. API_ORIGIN (build argument) is the API that /api/* is rewritten
# to; Next.js fixes rewrites at build time. The local demo builds with http://api:8000, so the
# panel and the API share one origin as they do behind the edge. Cloud Run and deploy/selfhost
# build without it: there the edge Worker or Caddy sends /api/* to the api service (§22).

FROM node:24.22.1-trixie-slim AS deps
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

FROM node:24.22.1-trixie-slim AS builder
WORKDIR /web
ARG API_ORIGIN=
ENV NEXT_TELEMETRY_DISABLED=1 \
    API_ORIGIN=$API_ORIGIN
COPY --from=deps /web/node_modules ./node_modules
COPY web/ ./
RUN npm run build

FROM node:24.22.1-trixie-slim AS runtime
RUN groupadd --system --gid 10001 kalkan \
    && useradd --system --uid 10001 --gid kalkan --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin kalkan
WORKDIR /app
COPY --from=builder --chown=root:root /web/.next/standalone ./
COPY --from=builder --chown=root:root /web/.next/static ./.next/static
COPY --from=builder --chown=root:root /web/public ./public
ARG GIT_SHA=dev
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    GIT_SHA=$GIT_SHA
USER kalkan
EXPOSE 3000
CMD ["node", "server.js"]
