# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    x11vnc \
    fluxbox \
    novnc \
    ca-certificates \
    gosu \
    fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY browser/requirements.txt /app/browser/requirements.txt
RUN pip install --no-cache-dir -r /app/browser/requirements.txt

COPY browser /app/browser

RUN chmod +x /app/browser/start.sh && \
    groupadd --system app && \
    useradd --system --gid app --home /app --shell /bin/bash app && \
    chown -R app:app /app

ENV PORT=8080
EXPOSE 8080

CMD ["bash", "/app/browser/start.sh"]
