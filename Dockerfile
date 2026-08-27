FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    SEEN_FILE=/data/seen_ids.json \
    HEARTBEAT_FILE=/data/heartbeat

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py .
USER appuser

CMD ["python", "-u", "watcher.py"]