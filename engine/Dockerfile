FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf XDG_CACHE_HOME=/models/cache NUMBA_CACHE_DIR=/tmp/numba \
    PYTHONPATH=/srv:/srv/vendor

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# Yalnız CPU'luk PyTorch (~200 MB)
RUN pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY vendor /srv/vendor
COPY engine /srv/engine

RUN useradd --create-home --uid 10001 tini && mkdir -p /models && chown -R tini:tini /models
USER tini
EXPOSE 9000
VOLUME ["/models"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS http://localhost:9000/health || exit 1

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1", "--timeout-keep-alive", "120"]
