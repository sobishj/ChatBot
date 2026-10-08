# One image for both the app (admin UI + public API) and the worker.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

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

USER appuser
EXPOSE 8000 8001
CMD ["python", "-m", "app.server"]
