# Digest-pinned for reproducible, integrity-checked base image (tag: 22-alpine).
FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2

# Install system deps required by Playwright Chromium
RUN apk add --no-cache \
    chromium \
    nss \
    freetype \
    harfbuzz \
    ca-certificates \
    ttf-freefont \
    wget \
    curl

# Tell Playwright to use the system Chromium instead of downloading its own
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-browser

WORKDIR /app

# Use the committed lockfile for reproducible, integrity-checked installs.
# npm ci fails if package.json and package-lock.json are out of sync, so caret
# ranges can no longer silently resolve to a new release at build time.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY server.mjs ./

EXPOSE 3003

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD wget -qO- http://localhost:3003/health || exit 1

CMD ["node", "server.mjs"]
