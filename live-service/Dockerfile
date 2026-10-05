# syntax=docker/dockerfile:1
#
# Контекст сборки — корень репозитория, поэтому пути с префиксом live-service/.
# Это позволяет запускать `fly launch` из корня, а fly.toml ссылается сюда.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    F1_CACHE_DIR=/data/cache \
    F1_POLL_SECONDS=3

WORKDIR /app

COPY live-service/requirements.txt .
RUN pip install -r requirements.txt

COPY live-service/app.py live-service/livefeed.py live-service/livews.py ./

# В режиме только-live (F1_ARCHIVE_FALLBACK=0) сервис занимает ~112 МБ
# рабочей памяти и почти не грузит CPU — влезает в бесплатный тариф 512 МБ.
# Если включить F1_ARCHIVE_FALLBACK=1, FastF1 начинает качать телеметрию
# всей сессии и требует ~780 МБ, тогда нужен тариф от 1 ГБ.
EXPOSE 8080

# Один воркер: состояние держится в памяти процесса, делить нечего.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]