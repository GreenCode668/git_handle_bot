FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BACKUP_PATH=/data/backups \
    DATABASE_PATH=/data/bot.db \
    WORK_PATH=/data/work

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ghbot \
    && mkdir -p /data && chown ghbot:ghbot /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ghbot ./ghbot

USER ghbot
VOLUME ["/data"]
CMD ["python", "-m", "ghbot"]
