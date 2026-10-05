# F1 Live Timing Service

Микросервис для вкладки **Live View**. Читает официальную ленту F1 реального
времени через [FastF1](https://github.com/theOehrly/Fast-F1) и отдаёт компактный
JSON с открытым CORS, чтобы статический сайт на GitHub Pages мог опрашивать его
из браузера.

## Почему нужен сервер

F1 публикует live timing бесплатно и без ключа — анонимный запрос
`POST https://livetiming.formula1.com/signalrcore/negotiate` возвращает
`connectionToken` и WebSocket-транспорт. Но в ответе **нет заголовков
`Access-Control-Allow-Origin`**, поэтому браузер не может читать данные напрямую.
GitHub Actions и GitHub Pages — статика, постоянно работающего процесса там нет.

Этот сервис — тонкая прокси-обёртка между F1 и браузером.

## Что отдаёт

`GET /api/snapshot` (или `/api/session`) — текущий снимок сессии:

| Поле | Что это |
|---|---|
| `positions` | реальные позиции пилотов |
| `cars[]` | координаты X/Y на трассе, скорость, газ, DRS, передача |
| `gaps` | отставание от лидера |
| `best_laps` | лучший круг каждого пилота |
| `tyres` | текущая резина |
| `weather` | температура воздуха и асфальта, влажность, ветер, давление |
| `race_control[]` | флаги, сейфти-кар, сообщения дирекции |
| `track_status` | статус трассы (GREEN / YELLOW / SAFETY CAR) |

## Запуск локально

```powershell
cd live-service
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload --port 8080
```

Проверка:

```powershell
curl.exe http://localhost:8080/health
curl.exe http://localhost:8080/api/status
```

> Первый `load()` занимает 30–90 секунд и ест ~1 ГБ памяти: FastF1 собирает
> DataFrame телеметрии за всю сессию. Дальше работает из кэша в `.ffcache`.

## Развёртывание

### Вариант 1 — Fly.io (рекомендуется)

Есть постоянные машины с 512 МБ RAM. Нужно минимум 1 ГБ, поэтому берём `shared-cpu-2x`.

```powershell
fly auth login
fly launch --no-deploy --copy-config --name f1-live
fly scale count 1 --vm-size shared-cpu-2x --vm-memory 1024
fly volumes create f1_cache --size 1 --region fra
fly deploy
```

Требуется кредитная карта (злые птицы не едят бесплатно), но на тарифе `pay-as-you-go` платите только за фактическое время работы. При 512 МБ × 24 ч сервис будет выключен через несколько дней.

### Вариант 2 — VPS

Любой VPS от ~4 €/мес (Hetzner, Contabo, DigitalOcean):

```bash
git clone <ваш-репозиторий> && cd f1-calendar/live-service
docker build -t f1live .
docker run -d --name f1live -p 8080:8080 \
  -v /srv/f1cache:/data/cache --restart unless-stopped f1live
```

### Вариант 3 — Render / Koyeb / Railway

Все три умеют Docker из репозитория. Укажите:

- Dockerfile: `live-service/Dockerfile`
- Health check: `/health`
- Instance type: не меньше 1 ГБ RAM

Бесплатные тарифы засыпают после 15 минут простоя и дают мало RAM — для теста
сойдёт, для постоянной работы нет.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `F1_CACHE_DIR` | `.ffcache` | каталог кэша FastF1 |
| `F1_POLL_SECONDS` | `2.5` | период опроса ленты |
| `F1_TELEMETRY_DISTANCE` | `50` | дистанция телеметрии (меньше — меньше памяти) |

## Подключение к сайту

Укажите адрес сервиса в атрибуте data-файла на странице Live View:

```html
<div class="live-page" data-live-service="https://f1-live.fly.dev">
```

Если сервис недоступен, вкладка Live View молча работает на статических данных
(задержка ~30 минут) — деградация без поломок.

## Ограничения

- **Некоммерческое использование только.** Данные F1 — для фанатских проектов.
- **RAM.** Полная телеметрия гонки — сотни мегабайт в памяти. На 512 МБ
  процесс убьётся; нужно 1 ГБ.
- **F1 не даёт историю через live-ленту.** Для архива используется OpenF1.
- Снимок обновляется раз в `F1_POLL_SECONDS`, это REST, не WebSocket: для
  ~20 пилотов хватает, но события быстрее 2–3 секунд не передаются.