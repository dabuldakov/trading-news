# trading-news

Отдельный конвейер **справочника и новостей MOEX**. Живёт независимо от торгового ядра:
свой репозиторий, свой compose-проект (`news`), своя Postgres, свой CI и деплой.
Основной `/opt/trading` и его воркеры не рестартятся.

## Что уже есть

- **reference**: суточный синк MOEX ISS (TQBR) → `instruments` / `companies` / `aliases`.
- опционально — флаги брокера (`short_enabled`, `broker_available`) из `/internal/instruments` основного проекта.
- **API**: `/health`, `/internal/instruments`, `/internal/companies`, `/internal/sync`.
- **news**: RSS-источники (ЦБ, РБК, Ведомости, Коммерсантъ, ТАСС, Интерфакс, Прайм, Финам, Smart-lab),
  дедуп по hash, таблицы `news_articles` / `news_sources`.
- заглушка NLP (`news_analysis`), сигналы — следующий этап.
- тесты (Postgres + pgvector в CI), Dockerfile, compose, CI/deploy.

## Быстрый старт

```bash
cp .env.example .env
# обязательно поменять POSTGRES_PASSWORD и SERVICE_KEY (openssl rand -hex 32)

docker compose -p news up -d --build
curl http://127.0.0.1:8001/health
curl -H "X-Service-Key: $SERVICE_KEY" http://127.0.0.1:8001/internal/instruments
```

## Локально

```bash
make install
docker compose -p news up -d postgres
export DATABASE_URL=postgresql://news:news@127.0.0.1:5433/news
make test
```

## Интеграция с основным проектом (три одноразовых шага)

1. **Справочник**: в основном проекте добавить `GET /internal/instruments` (service-key).
   Здесь задать `MAIN_API_URL` / `MAIN_SERVICE_KEY` — reference подтянет брокерские флаги.
2. **Caddy**: в основной Caddyfile добавить `import /etc/caddy/sites/*.caddy`, положить
   `deploy/Caddyfile.news` как `/etc/caddy/sites/news.caddy`, затем `caddy reload` (без рестарта).
3. **Сигналы**: в основном проекте добавить движок `external-signal-v1`, который читает
   `GET /internal/signals` (контракт — в `docs/ARCHITECTURE.md`).

После этого news деплоится независимо: `docker compose -p news up -d --build` не трогает
`/opt/trading`, а основной деплой не трогает news.

## Деплой

GitHub secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH`.

На сервере один раз:

```bash
mkdir -p /opt/news
# положить /opt/news/.env (не в git)
```

Пуш в `main` → CI → deploy workflow (`$DEPLOY_PATH`, `docker compose -p news up -d --build`). При первом деплое workflow сам создаёт `.env` со случайными `POSTGRES_PASSWORD` и `SERVICE_KEY` (посмотреть: `ssh ... 'cat /opt/news/.env'`).

## Дорожная карта

1. reference (готово).
2. news RSS/Telegram/GDELT + бэкфилл за год.
3. entity linking + rubert/LLM (sentiment / event_type / materiality).
4. event study и маркеры → University.
5. `external-signal-v1` → paper → live.
