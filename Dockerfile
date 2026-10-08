# ---- Stage 1: build the React admin UI ----------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python app (admin API + public API) and worker -------------------
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # LiteLLM: use the bundled model price list, never fetch it from the internet.
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    # Hugging Face: no telemetry.
    HF_HUB_DISABLE_TELEMETRY=1

# PostgreSQL 16 client tools (pg_dump for backups) from the official PGDG repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-common ca-certificates \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU-only PyTorch (much smaller than the default CUDA build).
RUN pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu

# Install dependencies first so code changes don't invalidate this layer.
ARG INSTALL_DEV=false
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; fi

# Run as an unprivileged user. /data is a volume (uploads, logos, model cache);
# creating it here gives a fresh named volume the right owner.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data /mnt/watched \
    && chown appuser:appuser /data

COPY --chown=appuser:appuser . .
COPY --from=frontend --chown=appuser:appuser /frontend/dist /srv/frontend/dist

USER appuser
EXPOSE 8000 8001
CMD ["python", "-m", "app.server"]
