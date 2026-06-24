FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_FILE_V2=/data/state-v2.json

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py links.py main.py max_api.py state.py ./

USER appuser

CMD ["python", "main.py"]
