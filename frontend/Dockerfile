# syntax=docker/dockerfile:1
# Arkray CRM frontend image (Next.js standalone server).

# Base images are pinned by digest (see backend/Dockerfile).
FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS base
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable

# --- dependencies (cached separately from source) --------------------------------------
FROM base AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# --- build -----------------------------------------------------------------------------
FROM base AS build
WORKDIR /app
# Rewrites are resolved at build time: where the Next.js server proxies /api/*.
ARG API_ORIGIN=http://backend:8000
ENV API_ORIGIN=${API_ORIGIN}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

# --- runtime: standalone server only, non-root -----------------------------------------
FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
# OS security updates published since the base image was built (Phase 9 review).
RUN apk upgrade --no-cache
RUN addgroup -S -g 10001 arkray && adduser -S -u 10001 -G arkray arkray
# Owned by root, read-only to the runtime user; only Next.js's own cache is writable.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
RUN mkdir -p .next/cache && chown arkray:arkray .next/cache
USER arkray
EXPOSE 3000
CMD ["node", "server.js"]
