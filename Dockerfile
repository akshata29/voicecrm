# VoiceCRM - ONE container: the FastAPI backend (voicecrm.api.main:app) also serves the built React SPA.
#
#   docker build -t voicecrm .
#   docker run --rm -p 8010:8000 voicecrm        # mock/local adapters by default -> http://127.0.0.1:8010 (host 8010 -> container 8000)
#
# Azure: azd builds this image remotely in Azure Container Registry (azure.yaml: docker.remoteBuild: true).
# If Docker Hub rate-limits that build, point the base images at a mirror with docker.buildArgs in azure.yaml
# (NODE_IMAGE / PYTHON_IMAGE).
ARG NODE_IMAGE=node:22-slim
ARG PYTHON_IMAGE=python:3.11-slim

# ---- Stage 1: build the SPA --------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS web
WORKDIR /build/frontend
# Dependency layer first. The lockfile is optional so a fresh checkout without one still builds.
COPY frontend/package.json frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python runtime -------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

LABEL org.opencontainers.image.title="voicecrm" \
      org.opencontainers.image.description="VoiceCRM: conversation-to-CRM demo (FastAPI API + React SPA, synthetic data)"

WORKDIR /app

# Unprivileged runtime user.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app

# Editable install keeps voicecrm.config.REPO_ROOT == /app, so everything resolved relative to the repo root
# (data/, frontend/dist/) lands where this image puts it.
COPY backend/ /app/backend/
RUN pip install -e /app/backend

COPY data/ /app/data/
COPY --from=web /build/frontend/dist /app/frontend/dist

# Writable state (SQLite databases, local vault) - per replica and ephemeral in Azure Container Apps.
RUN mkdir -p /app/state && chown app:app /app/state

# Explicit, so the app does not depend on how the package was installed.
ENV VOICECRM_DATA_DIR=/app/data \
    VOICECRM_STATE_DIR=/app/state \
    VOICECRM_SPA_DIR=/app/frontend/dist

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]

# One worker: the SQLite state is per process. Container Apps terminates TLS at the ingress and forwards X-Forwarded-*.
CMD ["uvicorn", "voicecrm.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
