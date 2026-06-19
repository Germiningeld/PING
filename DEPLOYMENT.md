# PING Deployment Runbook

Этот документ описывает безопасную подготовку PING к публикации и будущему ручному запуску на серверах. Он не является фактом production deployment: реальные серверы, DNS, HTTPS и production secrets на этом шаге не настраиваются.

## MVP Scope

PING состоит из двух частей:

- `central` - FastAPI application с `/health`, authenticated probe API, SQLite persistence, retention raw results и admin dashboard.
- `probe` - lightweight agent, который получает config от central, проверяет сайты через HTTP `GET` без редиректов и отправляет результаты обратно.

MVP рассчитан на небольшой внутренний dashboard: до 10 сайтов, 3 datacenter probes и одну проверку в минуту.

## Что Не Делает Этот Runbook

- Не выполняет production deployment.
- Не настраивает реальные домены, DNS, HTTPS или reverse proxy.
- Не создает и не публикует production secrets.
- Не устанавливает Docker или системные пакеты на серверах.
- Не добавляет monitoring, notifications, backup implementation или UI управления sites/probes.

## Pre-Publication Checklist

Перед публикацией repository проверьте:

```powershell
git status --short
git diff --stat
git diff --cached --stat
```

Убедитесь, что в Git не попадают:

- `.env` и любые `.env.*`, кроме `.env.example`;
- `probe-config.json`;
- `data/`, `storage/`, локальные SQLite databases и runtime queues;
- приватные ключи, certificates, dumps, реальные токены и пароли;
- `AGENTS.md`, `docs/`, `.agents/`, `.codex/`, если проектная память остается непубличной.

Проверьте public examples:

```powershell
Get-Content .env.example
Get-Content configs\probe-config.example.json
```

В них должны быть только placeholders, например `<pbkdf2-password-hash>`, `replace-with-random-session-secret` и `replace-with-probe-token`.

Запустите безопасные проверки:

```powershell
python -m pytest
docker compose -f docker-compose.central.yml config
docker compose -f docker-compose.probe.yml config
```

## Данные От Человека Перед Реальным Deployment

До реального серверного запуска нужно отдельно получить и утвердить:

- central VPS host/IP и SSH username;
- probe server host/IP и SSH username для каждого региона;
- домен или subdomain для central dashboard;
- DNS provider и ответственного за DNS;
- выбранный reverse proxy/HTTPS подход, например `Caddy`, `nginx + certbot` или provider-managed TLS;
- первые sites: `name`, `url`, `enabled`;
- probes: `probe_id`, `name`, `region`, `network_label`;
- способ хранения production secrets;
- решение по backup SQLite database.

Не передавайте в чат и не коммитьте:

- GitHub personal access token;
- приватные SSH keys;
- root password;
- реальные admin password, session secret или probe tokens;
- production `.env` и production `probe-config.json`.

## Central ENV

На сервере central нужен локальный `.env`, который не коммитится:

```text
PING_DATABASE_PATH=/data/ping.sqlite3
PING_ADMIN_USERNAME=<admin-username>
PING_ADMIN_PASSWORD_HASH=<pbkdf2-password-hash>
PING_ADMIN_SESSION_SECRET=<random-session-secret>
PING_COOKIE_SECURE=true
PING_RETENTION_DAYS=90
PING_ENV=production
```

Hash admin password генерируется локально. Не публикуйте исходный пароль и production hash в переписке:

```powershell
python -c "from central.app.auth import hash_admin_password; print(hash_admin_password('replace-with-real-password'))"
```

## Probe Config

На каждом probe server нужен локальный `probe-config.json`, который не коммитится:

```json
{
  "probe_id": "ru-dc-1",
  "probe_token": "<real-probe-token>",
  "central_api_url": "https://ping.example.com",
  "storage_dir": "/data/probe"
}
```

`probe_token` должен соответствовать token hash, записанному в central database для этого probe.

## Ручной Запуск Central

Эти команды предназначены для будущего запуска на подготовленном сервере после отдельного подтверждения человека.

1. Подключиться к central VPS по SSH.
2. Убедиться, что Docker и Docker Compose plugin установлены.
3. Разместить проект в выбранном каталоге, например `/opt/ping`.
4. Создать локальный `.env` на основе `.env.example`.
5. Создать persistent directory или volume для SQLite database.
6. Запустить central:

```bash
docker compose -f docker-compose.central.yml up --build -d
```

7. Проверить контейнер и логи:

```bash
docker compose -f docker-compose.central.yml ps
docker compose -f docker-compose.central.yml logs --tail=100 central
```

8. Проверить health локально на сервере:

```bash
curl -fsS http://localhost:8000/health
```

9. Настроить reverse proxy и HTTPS отдельным подтвержденным шагом.
10. Проверить dashboard через HTTPS URL.

## Ручной Запуск Probe

1. Подключиться к probe server по SSH.
2. Убедиться, что Docker и Docker Compose plugin установлены.
3. Разместить проект или нужный compose/build context.
4. Создать локальный `probe-config.json` с реальными `probe_id`, `probe_token` и `central_api_url`.
5. В `docker-compose.probe.yml` заменить example mount на локальный config:

```yaml
./probe-config.json:/config/probe-config.json:ro
```

6. Запустить probe:

```bash
docker compose -f docker-compose.probe.yml up --build -d
```

7. Проверить контейнер и логи:

```bash
docker compose -f docker-compose.probe.yml ps
docker compose -f docker-compose.probe.yml logs --tail=100 probe
```

8. Проверить, что central принимает новые results от этого probe.

## Проверки После Запуска

- `/health` central отвечает `{"status":"ok","service":"central"}`.
- `/dashboard` требует login.
- Admin login работает через production credentials.
- В dashboard видны sites и probes.
- Probe logs не показывают постоянные auth errors.
- В central database появляются новые `check_results`.
- Retention cleanup удаляет raw results старше `PING_RETENTION_DAYS` после приема batch results.
- `docker compose logs --tail=100` не показывает повторяющиеся runtime errors.

## Stop, Restart И Rollback

Без destructive действий можно использовать:

```bash
docker compose -f docker-compose.central.yml ps
docker compose -f docker-compose.central.yml logs --tail=100 central
docker compose -f docker-compose.central.yml restart central
docker compose -f docker-compose.central.yml stop central
docker compose -f docker-compose.central.yml start central
```

Для probe:

```bash
docker compose -f docker-compose.probe.yml ps
docker compose -f docker-compose.probe.yml logs --tail=100 probe
docker compose -f docker-compose.probe.yml restart probe
docker compose -f docker-compose.probe.yml stop probe
docker compose -f docker-compose.probe.yml start probe
```

Минимальный rollback без удаления данных: вернуть предыдущий commit или образ проекта, затем выполнить `up --build -d` с теми же volumes и локальными configs.

Не выполнять без отдельного подтверждения:

```text
docker compose down
docker compose down -v
docker system prune
rm -rf
```

## Production Risks

- SQLite подходит для MVP, но перед production нужен понятный backup process.
- В MVP нет notifications, поэтому инциденты видны только при открытом dashboard.
- В MVP нет UI управления sites/probes; первичная настройка идет через seed/config/database operations.
- Dev seed behavior нужно отдельно проверить перед production hardening.
- Dashboard нельзя безопасно открывать в интернет без reverse proxy и HTTPS.
