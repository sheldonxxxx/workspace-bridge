# syntax=docker/dockerfile:1
# Override with a reviewed digest for reproducible deployments.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
FROM node:24-bookworm-slim AS web-builder
WORKDIR /source/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
# Dependency-only layer: resolve third-party wheels from pyproject metadata
# without any Workspace Bridge source, so source/static/test edits do not
# invalidate this slow layer. The build backend is installed once here so the
# later source-only wheel build can run offline with --no-build-isolation.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]))' > /tmp/bridge-requirements.txt \
    && python -m pip wheel --wheel-dir /wheels -r /tmp/bridge-requirements.txt \
    && python -m pip install "setuptools>=77"
COPY README.md LICENSE ./
COPY workspace_bridge/ ./workspace_bridge/
COPY --from=web-builder /source/workspace_bridge/static/dist/ ./workspace_bridge/static/dist/
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 WB_STATE_DIR=/state HOME=/tmp
COPY --from=builder /wheels /wheels
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels workspace-bridge==0.8.4 \
    && rm -rf /wheels \
    && groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp bridge \
    && mkdir -p /state /opt/workspace-bridge \
    && chown 10001:10001 /state
WORKDIR /opt/workspace-bridge
USER 10001:10001
EXPOSE 8765 8766
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD ["python", "-m", "workspace_bridge.container_health"]
ENTRYPOINT ["python", "-m", "workspace_bridge.docker_entrypoint"]
CMD ["serve"]
