# PING

PING - внутренний инструмент мониторинга доступности клиентских сайтов.

## Структура Проекта

- `central/` - центральное FastAPI-приложение. Сейчас содержит минимальный application и `/health`.
- `probe/` - будущий lightweight probe agent. Сейчас это безопасный stub без сетевых запросов, локального cache/config и runtime-логики мониторинга.
- `tests/` - базовые тесты импортов, `/health` и stub `probe`.

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

Dev-контейнер монтирует исходный код приложения и запускает FastAPI через `uvicorn --reload`. После изменения Python-файлов приложение должно автоматически перезагрузиться без ручного перезапуска контейнера или процесса.

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

### Проверки

```powershell
pytest
```

### Запуск Probe Stub

Stub можно запустить без реальных сетевых запросов:

```powershell
python -m probe.app.cli
```

После установки проекта в editable-режиме также доступен console script:

```powershell
ping-probe
```

Ожидаемый смысл вывода: `probe` пока является stub и не выполняет реальные HTTP-проверки сайтов, синхронизацию с central API, локальный cache/config или очередь результатов.
