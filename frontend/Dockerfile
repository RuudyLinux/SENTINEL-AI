# SENTINEL VISION dashboard (Next.js 16, App Router).
#
# Multi-stage so the runtime image carries the built output and production
# dependencies only — not the full node_modules tree used to compile it.

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# The API base is read at BUILD time by Next for statically rendered pages, so
# it has to be present here, not only at runtime. Defaults to the compose
# service URL; override with --build-arg for another deployment.
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ARG NEXT_PUBLIC_WS_BASE=ws://localhost:8000
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE \
    NEXT_PUBLIC_WS_BASE=$NEXT_PUBLIC_WS_BASE
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/next.config.js ./next.config.js
# No `public/` copy: this project has no public directory (the one static
# asset, the favicon, lives at app/icon.svg and is emitted into .next by the
# build). COPYing a non-existent path would fail the build.

# `node` already exists in this base image as a non-root user.
USER node
EXPOSE 3000

# `wget` is busybox's built-in applet on Alpine — already present, no `apk add`
# needed. `/login` is a public, unauthenticated route so this needs no
# credentials. Without this, `docker compose up --wait` can only confirm the
# container is RUNNING, never that Next.js is actually SERVING — a broken
# `next start` (bad env, missing build output) would still read as healthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:3000/login || exit 1

CMD ["npm", "run", "start"]
