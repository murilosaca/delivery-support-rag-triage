FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ANONYMIZED_TELEMETRY=FALSE \
    TOKENIZERS_PARALLELISM=false \
    CHROMA_PERSIST_DIR=/data/chroma_db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app
COPY data ./data

RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser \
    && mkdir -p /data/chroma_db \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
