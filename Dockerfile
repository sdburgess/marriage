FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CONFIG_PATH=/data/config.yaml \
    STATE_PATH=/data/state.json \
    LOG_LEVEL=INFO

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY cupid ./cupid

# /data is mounted as a volume on fly.io so state.json + config.yaml persist
VOLUME ["/data"]

CMD ["python", "-m", "cupid", "run"]
