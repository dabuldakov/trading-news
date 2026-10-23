# Архитектура trading-news

## Принцип

Новости — отдельная единица деплоя. Основной трейдинг не должен рестартиться при
изменениях news. Связь — через два внутренних endpoint'а и один контракт сигналов.

```
Брокеры (T-Bank/BCS) ──1/сутки──► main: /internal/instruments
MOEX ISS (канон) ───────────────► reference ──► Postgres (instruments/companies/aliases)
                                                   │
RSS/Telegram/GDELT ──ежечасно──► news ingest ──────┤
                                                   ▼
                                          nlp (rubert/LLM)
                                                   │
                                                   ▼
                                    research (event study / markers)
                                                   │
                                                   ▼
                              news: /internal/signals ──► main: external-signal-v1
```

## Границы

| Компонент | Где | Рестарт основного |
|---|---|---|
| reference / news / nlp / api | `/opt/news`, проект `news` | нет |
| Postgres + pgvector | `/opt/news` (свой volume) | нет |
| `/internal/instruments` | основной проект | один раз |
| `external-signal-v1` | основной проект | один раз |
| Caddy `import sites/*.caddy` | основной Caddy | `caddy reload`, без рестарта |

## Контракт сигналов (v1)

`GET /internal/signals?since=<iso>` с заголовком `X-Service-Key`:

```json
{
  "signals": [
    {
      "id": 42,
      "ticker": "SBER",
      "side": "buy",
      "expected_edge": 0.012,
      "confidence": 0.62,
      "horizon": "1d",
      "marker_key": "dividend|positive|interfax",
      "event_id": 123,
      "ts": "2026-10-23T07:05:00+00:00"
    }
  ]
}
```

Правила:

- `side` ∈ `buy|sell`; `expected_edge` — ожидаемая доходность после издержек.
- `horizon` ∈ `1h|1d|3d|5d`.
- `marker_key` — идентификатор исторического маркера, по которому посчитан edge.
- Основной движок сам применяет риск/сайзинг/paper-live; news не размещает заявки.

## Данные

- `instruments` — тикер, ISIN, лот, валюта, режим, short/broker flags, статус.
- `companies` / `aliases` — эмитент и все варианты названия (для entity linking).
- `news_articles` — published_at, source, title/body, hash, embedding (pgvector).
- `news_tickers` — статья → тикер (confidence, method).
- `news_analysis` — sentiment, event_type, materiality, surprise, horizon.
- `news_reactions` — реакции цены (1м…5д), MFE/MAE, volume_z.
- `event_markers` — агрегированная статистика маркеров.
- `signals` — то, что читает основной движок.

## Правила разработки

- Все внешние вызовы — с таймаутами/retry; один упавший источник не рвёт цикл.
- Дедуп статей: hash(title+url) сейчас, embedding+pg_trgm — на этапе 2.
- Look-ahead запрещён: реакции считаются только по времени `published_at`.
- Секреты только в `/opt/news/.env`; в git — `.env.example`.
- Миграции — только в `migrations/*.sql`, применяются автоматически при старте.
