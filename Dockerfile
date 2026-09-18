# syntax=docker/dockerfile:1
# Override with a reviewed digest for reproducible deployments.
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY workspace_bridge/ ./workspace_bridge/
RUN python -m pip wheel --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 WB_STATE_DIR=/state HOME=/tmp
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels workspace-bridge==0.7.0 \
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
