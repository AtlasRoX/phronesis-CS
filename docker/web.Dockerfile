# syntax=docker/dockerfile:1
#
# Phronesis web app (Next.js).
#
#   docker build -f docker/web.Dockerfile --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000 \
#     -t phronesis-web web            (context = web/)
#
# The browser calls the API directly: web/src/app/page.tsx reads NEXT_PUBLIC_API_URL (default
# http://127.0.0.1:8000), and Next.js inlines NEXT_PUBLIC_* at build time. Set the build arg to the URL the
# *browser* can reach, not the compose service name, and list the web origin in the backend's
# PHRONESIS_CORS_ORIGINS. Changing the URL needs a rebuild (the in-app settings field can override it per user).

ARG NODE_VERSION=22

# ---------------------------------------------------------------------------------------------------------
# deps: install exactly what package-lock.json pins.
# ---------------------------------------------------------------------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS deps
WORKDIR /web
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

# ---------------------------------------------------------------------------------------------------------
# build: next build, then drop dev dependencies for the runtime copy.
# ---------------------------------------------------------------------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS build
WORKDIR /web
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_DEFAULT_MODEL=
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_DEFAULT_MODEL=${NEXT_PUBLIC_DEFAULT_MODEL} \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /web/node_modules ./node_modules
COPY . .
RUN npm run build && npm prune --omit=dev --no-audit --no-fund

# ---------------------------------------------------------------------------------------------------------
# runtime: next start on 3000 as the unprivileged `node` user.
# ---------------------------------------------------------------------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS runtime
WORKDIR /web
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /web/package.json /web/package-lock.json ./
COPY --from=build --chown=node:node /web/node_modules ./node_modules
COPY --from=build --chown=node:node /web/.next ./.next
COPY --from=build --chown=node:node /web/public ./public
COPY --from=build --chown=node:node /web/next.config.ts ./next.config.ts
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["npx", "--no-install", "next", "start", "-H", "0.0.0.0", "-p", "3000"]
