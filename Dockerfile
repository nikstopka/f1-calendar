# syntax=docker/dockerfile:1
#
# F1 Live Timing Service.
#
# Dockerfile лежит в корне репозитория намеренно: это путь по умолчанию для
# Railway, Render и Fly, поэтому deploy работает без ручной настройки. Контекст
# сборки — тоже корень, поэтому файлы берутся с префиксом live-service/.
#
#   docker build -t f1live .                                   # лёгкий образ
#   docker build --build-arg ARCHIVE=1 -t f1live .              # + FastF1
#   docker run -d -p 8080:8080 f1live

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    F1_POLL_SECONDS=3 \
    F1_ARCHIVE_FALLBACK=0

WORKDIR /app

# Режим по умолчанию — только живой фид: WebSocket плюс HTTP-сервис, ~120 МБ
# образа. FastF1 не ставится: он нужен лишь для F1_ARCHIVE_FALLBACK=1, тянет
# ~400 МБ и во время трансляции не используется.
COPY live-service/requirements.txt .
RUN pip install -r requirements.txt

# Необязательный слой для архивного режима: FastF1 подтягивает телеметрию
# последней завершённой сессии, но требует ~780 МБ RAM.
COPY live-service/requirements-archive.txt ./requirements-archive.txt
ARG ARCHIVE=0
RUN if [ "$ARCHIVE" = "1" ]; then pip install -r requirements-archive.txt; fi

COPY live-service/app.py live-service/livefeed.py live-service/livews.py ./

EXPOSE 8080

# Один воркер: состояние живёт в памяти процесса, делить нечего.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]