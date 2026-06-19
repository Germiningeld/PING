# PING

PING - внутренний инструмент мониторинга доступности клиентских сайтов.

## Структура Проекта

- `central/` - центральное FastAPI-приложение. Сейчас содержит `/health`, SQLite persistence слой, authenticated probe API и admin dashboard shell.
- `probe/` - lightweight probe agent MVP: синхронизация config с central API, HTTP `GET` проверки без редиректов, локальный cache и очередь результатов.
- `tests/` - базовые тесты импортов, `/health`, persistence layer, probe API и probe agent.

## Локальная Разработка

### Требования

- Python `3.12+`
- Docker и Docker Compose для dev-запуска в контейнере

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

### Admin Dashboard

Dashboard доступен по адресу:

```text
http://localhost:8000/dashboard
```

Неавторизованный пользователь будет перенаправлен на `/login`.

Для локального запуска задайте admin credentials и session secret через environment variables или локальный `.env` файл на основе `.env.example`:

```text
PING_ADMIN_USERNAME=admin
PING_ADMIN_PASSWORD_HASH=<pbkdf2-password-hash>
PING_ADMIN_SESSION_SECRET=<random-session-secret>
PING_COOKIE_SECURE=false
```

Сгенерировать hash пароля можно через helper из проекта:

```powershell
python -c "from central.app.auth import hash_admin_password; print(hash_admin_password('replace-with-local-password'))"
```

Не коммитьте реальные значения `PING_ADMIN_PASSWORD_HASH` и `PING_ADMIN_SESSION_SECRET`.

### Проверки

```powershell
pytest
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
