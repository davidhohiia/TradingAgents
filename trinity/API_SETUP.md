# Подключение API

Общие правила:
* Пользуйтесь только площадками, **доступными в вашей юрисдикции**, и проверяйте наличие нужных пар.
* Ключ: права **только на чтение и спотовую торговлю**. **Вывод средств — выключен.** Фьючерсы/маржа — выключены.
  По возможности — ограничение по IP.
* Ключи вписываются только в `trinity/.env`. Не отправляйте их в чаты, Telegram, issue-трекеры.
* TRINITY никогда не запрашивает seed-фразы или приватные ключи кошельков.
* Названия пунктов меню бирж меняются — ориентируйтесь на официальную документацию каждой площадки.

## Криптобиржа (Crypto Momentum) — через CCXT

TRINITY использует единый API CCXT (https://docs.ccxt.com). Поддерживается любая биржа CCXT со спотом.

```
CRYPTO_EXCHANGE_ID=binance          # id биржи в CCXT: binance, kraken, okx, bybit, ...
CRYPTO_API_KEY=...
CRYPTO_API_SECRET=...
CRYPTO_API_PASSWORD=...             # только если биржа требует passphrase (OKX, KuCoin)
```

Перед LIVE проверьте по официальной документации выбранной биржи:
* поддержка клиентского id ордера (TRINITY передаёт `clientOrderId`; CCXT переводит его в формат биржи
  там, где это поддерживается);
* в какой валюте списывается комиссия (если в базовом активе — TRINITY зачисляет чистое количество);
* минимальный размер ордера (берётся из `load_markets`);
* ваш реальный уровень комиссий (в `instruments.yaml` — оценки 0.1%).

`python trinity/scripts/run_live.py --check` загрузит рынки, проверит пары и что ключ читает баланс.

## Изолированная маржа (только после разрешения на плечо)

Используются унифицированные методы CCXT `set_margin_mode('isolated', symbol)`, `set_leverage`,
`fetch_funding_rate`, `fetch_positions` для линейных USDT-перпетуалов (`BTC/USDT:USDT`), количество
пересчитывается через `contractSize`. Ключ должен разрешать фьючерсную торговлю — **только когда вы
сами решили включить плечо** после ≥ 60 дней реальной торговли без плеча; вывод средств всегда выключен.

```
CRYPTO_PERP_EXCHANGE_ID=binance      # пусто = CRYPTO_EXCHANGE_ID
LIVE_LEVERAGE_ENABLED=false
LEVERAGE_ACKNOWLEDGEMENT=
```
Проверка: `python trinity/scripts/leverage.py --status`. Адаптер реальной биржей **не проверялся**.

## Арбитраж — только публичные данные

```
ARB_EXCHANGE_IDS=binance,kraken,okx
```
Ключи не нужны и не используются. Реальных ордеров арбитражный бот не отправляет.
Комиссии и стоимость перевода в `strategies.yaml → arbitrage` — **оценки**, уточните для своих бирж.

## Брокер акций (Stock Trend) — Alpaca

Реализован адаптер Alpaca Trading API v2 и Market Data API v2 (документация: https://docs.alpaca.markets).
Используемые эндпоинты: `/v2/account`, `/v2/assets/{symbol}`, `/v2/orders`, `/v2/orders:by_client_order_id`,
`/v2/positions`, `/v2/clock`; данные: `data.alpaca.markets/v2/stocks/{symbol}/bars` и `/quotes/latest` (фид IEX).

```
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET_KEY=...
ALPACA_PAPER_ACCOUNT=true           # paper-api.alpaca.markets; для LIVE нужно false
ALPACA_DATA_FEED=iex
STOCK_DATA_PROVIDER=alpaca
```

Перед использованием `verify_capabilities` проверяет: доступность API, разрешение торговли, статус
счёта, отсутствие маржинального множителя (нужен кэш-счёт), доступность и дробность каждого инструмента.
Инструмент, неизвестный брокеру, считается недоступным. Если бумага не дробная — используются только
целые акции (при $150 бюджете дорогие ETF могут быть недоступны — бот так и сообщит).

Alpaca доступна не во всех странах. Если она вам недоступна, реализуйте свой адаптер, унаследовав
`exchanges/stock_broker.py::StockBroker` (методы рыночных данных, ордеров, позиций и
`verify_capabilities`), и проверьте его по официальной документации вашего брокера.

Альтернатива для бэктестов: CSV-файлы `trinity/data/csv/<SYMBOL>.csv` с колонками
`ts,open,high,low,close,volume` и `STOCK_DATA_PROVIDER=csv` (котировки будут условными — только для тестов).

## Telegram

`@BotFather` → `/newbot` → токен в `TELEGRAM_BOT_TOKEN`; свой числовой id — в
`TELEGRAM_ALLOWED_USER_IDS`. Используются официальные методы Bot API `getUpdates` и `sendMessage`.

## Статус проверки адаптеров

| Адаптер | Проверено |
|---|---|
| PaperExchange | да, автотесты и 24-ч симуляция |
| CCXTExchange | на имитации ответов CCXT (без сети); реальное подключение **не проверялось** |
| CCXTDerivativesExchange (изолированная маржа) | на имитации ответов CCXT; реальное подключение **не проверялось** |
| AlpacaBroker / AlpacaDataProvider | на имитации HTTP (httpx MockTransport); реальное подключение **не проверялось** |
| Telegram | на имитации HTTP; реальное подключение **не проверялось** |
