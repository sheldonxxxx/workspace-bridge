# syntax=docker/dockerfile:1
# Private client-only sidecar for the OpenCode SDK. It connects to an
# externally managed native OpenCode server; it never starts one and publishes
# no host port.
FROM node:22-alpine
ENV NODE_ENV=production NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --omit=dev --no-audit --no-fund
COPY sdk-runtime.mjs server.mjs adapter.mjs ./
USER node
EXPOSE 8770
CMD ["node", "adapter.mjs"]
