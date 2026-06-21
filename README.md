# PING

PING - внутренний инструмент мониторинга доступности клиентских сайтов из нескольких точек наблюдения. Central application собирает результаты от probe agents, хранит raw checks в SQLite и показывает admin-only dashboard с текущим статусом, историей за выбранную дату и диагностикой ошибок.

Проект готов как MVP/demo и Docker Compose example. Реальный production deployment, домен, DNS, HTTPS, reverse proxy и production secrets не настраиваются этим repository автоматически.

## MVP Состав

- Central FastAPI application с `/health`, admin login, dashboard, authenticated probe API и retention raw `check_results`.
- Probe agent для синхронизации config, HTTP `GET` проверок без редиректов, локального cache и очереди неотправленных results.
- SQLite persistence для MVP-объема: до 10 sites, 3 datacenter probes, одна проверка в минуту.
- Dockerfile и Docker Compose examples для central и probe.
- `.env.example` и `configs/probe-config.example.json` без реальных секретов.
- Public deployment runbook: `DEPLOYMENT.md`.

## MVP Ограничения

- Нет automatic production deployment.
- Нет настройки реального домена, DNS, HTTPS или reverse proxy.
- Нет notifications, monitoring, backup implementation и UI управления sites/probes.
- Секреты должны храниться только в локальных `.env`/config файлах и не коммититься.

## Структура Проекта

- `central/` - центральное FastAPI-приложение с `/health`, SQLite persistence, authenticated probe API и server-rendered dashboard.
- `central/app/templates/` - Jinja2 templates login/dashboard и presentation partials.
- `central/app/static/css/` - пользовательские стили web-слоя; локальный Bootstrap хранится отдельно в `central/app/static/`.
- `probe/` - lightweight probe agent MVP: синхронизация config с central API, HTTP `GET` проверки без редиректов, локальный cache и очередь результатов.
- `tests/` - базовые тесты импортов, `/health`, persistence layer, probe API и probe agent.
- `docker-compose.central.yml` - пример запуска central через Docker Compose.
- `docker-compose.probe.yml` - пример запуска probe через Docker Compose.
- `configs/probe-config.example.json` - пример локального config для probe без секретов.
- `DEPLOYMENT.md` - checklist публикации и runbook будущего ручного запуска без production secrets.

## Локальная Разработка

### Требования

- Python `3.12+`
- Docker и Docker Compose для dev-запуска в контейнере

### Local-only Central Без Авторизации

Для быстрой UI-разработки используйте отдельный Compose-файл. Он не подключает `.env`, не требует admin credentials или session secret и использует локальную SQLite-базу `data/dev-check.sqlite3` через bind mount:

```powershell
docker compose -f docker-compose.local.yml up --build -d
```

Последующие запуски, проверка состояния, короткие логи и health check:

```powershell
docker compose -f docker-compose.local.yml up -d
docker compose -f docker-compose.local.yml ps
docker compose -f docker-compose.local.yml logs --tail=100 central
Invoke-RestMethod http://localhost:8000/health
```

Dashboard открывается без cookie и формы входа:

```text
http://localhost:8000/dashboard
```

Каталоги `central/`, `probe/` и `tests/` подключены через bind mounts. Изменения Python-файлов в `central/` подхватывает `uvicorn --reload`, поэтому image пересобирать не нужно. После изменения `Dockerfile.dev`, `pyproject.toml` или зависимостей выполните явный rebuild:

```powershell
docker compose -f docker-compose.local.yml up --build -d
```

Отключение dashboard auth действует только при одновременных `PING_ENV=development` и `PING_AUTH_DISABLED=true`. В `production` один флаг `PING_AUTH_DISABLED=true` не отключает авторизацию. Probe API по-прежнему требует token auth.

### Запуск Без Docker

Создайте виртуальное окружение и установите зависимости:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Запустите central FastAPI application в dev-режиме:

```powershell
uvicorn central.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Запуск Через Docker Compose Dev

```powershell
docker compose -f docker-compose.dev.yml up --build
```

Dev-контейнер монтирует `central/`, `probe/` и `tests/`, затем запускает FastAPI через `uvicorn --reload`. После изменения Python-файлов в `central/` приложение должно автоматически перезагрузиться без ручного перезапуска контейнера или процесса.

### Проверка Health Endpoint

После запуска откройте:

```text
http://localhost:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok","service":"central"}
```

Через PowerShell можно проверить так:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

## Docker Compose Запуск MVP

Проект содержит отдельные примеры для central и probe. Они предназначены для воспроизводимого запуска MVP, но не настраивают реальный домен, HTTPS или production deployment.

Для публикации repository и будущего серверного запуска сначала прочитайте `DEPLOYMENT.md`: там перечислены pre-publication checklist, нужные доступы, запрещенные к передаче секреты, ручной порядок запуска, проверки, restart/stop и rollback-команды.

### Central

Соберите и запустите central:

```powershell
docker compose -f docker-compose.central.yml up --build -d
```

Проверьте состояние контейнера и короткий хвост логов:

```powershell
docker compose -f docker-compose.central.yml ps
docker compose -f docker-compose.central.yml logs --tail=100 central
```

Локальный URL:

```text
http://localhost:8000
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8000/health
```

### Probe

Скопируйте `configs/probe-config.example.json` в локальный config, который не коммитится, и замените placeholder token на реальный token для выбранного probe:

```powershell
Copy-Item configs\probe-config.example.json probe-config.json
```

В `docker-compose.probe.yml` по умолчанию смонтирован example config. Для реального локального запуска замените mount на `./probe-config.json:/config/probe-config.json:ro`.

Запуск:

```powershell
docker compose -f docker-compose.probe.yml up --build -d
```

Проверки:

```powershell
docker compose -f docker-compose.probe.yml ps
docker compose -f docker-compose.probe.yml logs --tail=100 probe
```

Probe-контейнер запускает `ping-probe --once` в 60-секундном цикле и хранит cache/queue в persistent volume `probe-data`.

### Важные Docker Команды

Для постоянного dev-окружения используйте `up -d`, `ps`, `logs --tail=100`, `exec` и точечный `restart`.

Не запускайте без явного решения:

```text
docker compose down
docker compose down -v
docker system prune
```

### Admin Dashboard

Dashboard доступен по адресу:

```text
http://localhost:8000/dashboard
```

Неавторизованный пользователь будет перенаправлен на `/login`.
После входа dashboard показывает список sites, текущий статус по probes,
график response time за выбранную дату в пределах 90-дневного retention,
детали HTTP status/error type и recent problems.

Для локального запуска задайте admin credentials и session secret через environment variables или локальный `.env` файл на основе `.env.example`:

```text
PING_ADMIN_USERNAME=admin
PING_ADMIN_PASSWORD_HASH=<pbkdf2-password-hash>
PING_ADMIN_SESSION_SECRET=<random-session-secret>
PING_COOKIE_SECURE=false
PING_RETENTION_DAYS=90
```

Сгенерировать hash пароля можно через helper из проекта:

```powershell
python -c "from central.app.auth import hash_admin_password; print(hash_admin_password('replace-with-local-password'))"
```

Не коммитьте реальные значения `PING_ADMIN_PASSWORD_HASH` и `PING_ADMIN_SESSION_SECRET`.

### Проверки

```powershell
python -m pytest
docker compose -f docker-compose.central.yml config
docker compose -f docker-compose.probe.yml config
```

### Запуск Probe Agent MVP

Probe agent читает локальный JSON config. Минимальный пример:

```json
{
  "probe_id": "ru-dc-1",
  "probe_token": "dev-token-ru-dc-1",
  "central_api_url": "http://localhost:8000",
  "storage_dir": "data/probe"
}
```

`probe_token` в реальном окружении является секретом, поэтому локальный config не должен попадать в Git.

MVP поддерживает один цикл `sync -> check -> submit`:

```powershell
python -m probe.app.cli --config .\probe-config.json --once
```

После установки проекта в editable-режиме также доступен console script:

```powershell
ping-probe --config .\probe-config.json --once
```

В `storage_dir` сохраняются:

- `sites-config.json` - последний успешно синхронизированный config сайтов;
- `results-queue.json` - результаты, которые не удалось отправить в central API.

## SQLite Persistence

SQLite schema и базовые операции находятся в `central/app/persistence.py`.

Слой данных умеет:

- инициализировать базу без ручных SQL-шагов;
- хранить `Site`, `Probe` и `CheckResult`;
- создавать индексы для будущих dashboard-запросов по `site_id`, `probe_id`, `checked_at` и `site_id + checked_at`;
- добавлять dev seed с примером сайта и трех datacenter probes без реальных секретов.
- удалять raw check results старше retention cutoff через `cleanup_check_results_older_than`.

Retention по умолчанию составляет 90 дней. Central API применяет cleanup после приема batch результатов от probe. Значение можно переопределить через `PING_RETENTION_DAYS`, но для MVP оно должно оставаться согласованным с PRD.

## Переменные Окружения

Required для central:

```text
PING_DATABASE_PATH=data/dev-check.sqlite3
PING_ADMIN_USERNAME=admin
PING_ADMIN_PASSWORD_HASH=<pbkdf2-password-hash>
PING_ADMIN_SESSION_SECRET=<random-session-secret>
PING_COOKIE_SECURE=false
PING_RETENTION_DAYS=90
```

Optional:

```text
PING_ENV=development
PING_HOST=0.0.0.0
PING_PORT=8000
```

Required для probe config:

```json
{
  "probe_id": "ru-dc-1",
  "probe_token": "replace-with-probe-token",
  "central_api_url": "http://localhost:8000",
  "storage_dir": "data/probe"
}
```

`probe_token` и `PING_ADMIN_SESSION_SECRET` являются секретами и не должны попадать в Git.

## MVP Seed И Config

В MVP список sites/probes можно подготовить через dev seed `seed_development_data` или через прямую инициализацию SQLite с функциями `create_site` и `create_probe` из `central.app.persistence`.

Dev seed создает:

- `https://example.com/`;
- probes `ru-dc-1`, `eu-dc-1`, `us-dc-1`;
- placeholder tokens вида `dev-token-<probe_id>`, которые хранятся в базе только как SHA-256 hash.

Для ручной подготовки локальной базы:

```powershell
python -c "from central.app.persistence import connect_database, initialize_database, seed_development_data; c=connect_database('data/dev-check.sqlite3'); initialize_database(c); seed_development_data(c); c.close()"
```

## Central Probe API

Probe API находится под prefix `/api/probe`.

- `GET /api/probe/config` - возвращает активные сайты и параметры проверки.
- `POST /api/probe/results` - принимает batch результатов проверок и сохраняет их в SQLite.

Оба endpoint требуют заголовки:

```text
X-Probe-Id: ru-dc-1
Authorization: Bearer <probe-token>
```

Токены хранятся в базе только как SHA-256 hash. Dev seed использует placeholder tokens вида `dev-token-<probe_id>` только для локальной проверки.
