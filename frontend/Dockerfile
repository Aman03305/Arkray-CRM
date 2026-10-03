# syntax=docker/dockerfile:1
# Arkray CRM frontend image (Next.js standalone server).

FROM node:24-alpine AS base
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
FROM node:24-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
RUN addgroup -S -g 10001 arkray && adduser -S -u 10001 -G arkray arkray
COPY --from=build --chown=arkray:arkray /app/.next/standalone ./
COPY --from=build --chown=arkray:arkray /app/.next/static ./.next/static
USER arkray
EXPOSE 3000
CMD ["node", "server.js"]
