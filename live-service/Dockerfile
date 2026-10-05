# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    F1_CACHE_DIR=/data/cache \
    F1_POLL_SECONDS=3

WORKDIR /app

# pandas/numpy wheels are large; keeping them in one layer speeds rebuilds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Telemetry for a full race is a few hundred MB in memory, so the container
# wants more headroom than a typical free default.
ENV F1_TELEMETRY_DISTANCE=30
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]