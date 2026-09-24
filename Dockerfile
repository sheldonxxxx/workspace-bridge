# syntax=docker/dockerfile:1
# Override with a reviewed digest for reproducible deployments.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG NODE_IMAGE=node:24-bookworm-slim

FROM ${NODE_IMAGE} AS web-builder
WORKDIR /source/web
# Dependency-only layer: reinstall only when package.json/lock changes.
# The npm cache mount keeps re-resolves fast without busting the layer.
COPY --link web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
# Build-inputs-only layer: test/lint/e2e config edits do not invalidate
# the slow `npm run build` step. Keep this list in sync with vite.config.ts
# inputs (index.html, vite/ts configs, components.json, public/, src/).
COPY --link web/index.html web/vite.config.ts web/components.json web/tsconfig.json web/tsconfig.app.json web/tsconfig.node.json ./
COPY --link web/public/ ./public/
COPY --link web/src/ ./src/
RUN npm run build

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
# Dependency-only layer: resolve third-party wheels from pyproject metadata
# without any Workspace Bridge source, so source/static/test edits do not
# invalidate this slow layer. uv.lock is copied as a cache key so lock bumps
# bust the layer even when the dependency ranges in pyproject.toml are unchanged.
# The build backend is installed once here so the
# later source-only wheel build can run offline with --no-build-isolation.
COPY --link pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' > /tmp/bridge-requirements.txt \
    && python -m pip wheel --wheel-dir /wheels -r /tmp/bridge-requirements.txt \
    && python -m pip install "setuptools>=77"
COPY --link README.md LICENSE ./
COPY --link workspace_bridge/ ./workspace_bridge/
COPY --link --from=web-builder /source/workspace_bridge/static/dist/ ./workspace_bridge/static/dist/
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 WB_STATE_DIR=/state HOME=/tmp
# OS/user layer first: cached across Python dependency and source changes.
# Previously this apt-get ran after COPY /wheels, so every dependency bump
# re-ran `apt-get update`. Splitting keeps the slow network step reusable.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp bridge \
    && mkdir -p /state /opt/workspace-bridge \
    && chown 10001:10001 /state
COPY --link --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels workspace-bridge==0.8.4 \
    && rm -rf /wheels
WORKDIR /opt/workspace-bridge
USER 10001:10001
EXPOSE 8765 8766
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD ["python", "-m", "workspace_bridge.container_health"]
ENTRYPOINT ["python", "-m", "workspace_bridge.docker_entrypoint"]
CMD ["serve"]
