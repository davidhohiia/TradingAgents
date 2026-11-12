# TRINITY AI Trading System

> ⚠️ **Важно.** TRINITY — инструмент для исследования и автоматизации торговли с контролем риска.
> Он **не гарантирует прибыль**. Торговля криптовалютами и акциями может привести к потере части
> или всего капитала. По умолчанию система работает **только в режиме виртуальной торговли (PAPER)**.
> Все результаты виртуальной торговли, бэктестов и симуляций — виртуальные и не предсказывают реальные.

Быстрый старт: [QUICKSTART.md](QUICKSTART.md) · Риски: [RISK_POLICY.md](RISK_POLICY.md) ·
Ключи API: [API_SETUP.md](API_SETUP.md) · Windows 24/7: [DEPLOYMENT_WINDOWS.md](DEPLOYMENT_WINDOWS.md)

---

## 1. Описание проекта

TRINITY — система из трёх независимых ботов под управлением центрального менеджера портфеля:

| Бот | Назначение | Капитал (из $500) | Режим по умолчанию |
|---|---|---|---|
| **Crypto Grid AI** (`crypto_grid`) | адаптивная сетка на BTC/USDT, ETH/USDT (спот, CCXT) | $150 | PAPER |
| **Stock Trend AI** (`stock_trend`) | трендследование EMA20/EMA50 на SPY, QQQ, VTI | $150 | PAPER |
| **Arbitrage AI** (`arbitrage`) | поиск межбиржевых расхождений цен | $0 | **только сканирование + виртуальные сделки** |
| Резерв | никогда не используется автоматически | $200 | — |

Приоритеты: сохранение капитала → корректность исполнения → безопасность → прозрачная статистика → стабильность 24/7.

Запрещено кодом и конфигурацией: кредитное плечо, маржа, шорт, опционы, CFD, мартингейл,
увеличение размера после убытков, обход Risk Manager, автоматическое увеличение торгового капитала,
автоматический переход в LIVE после перезапуска.

## 2. Архитектура системы

```
            Telegram ──┐          Streamlit dashboard (read-only DB)
                       ▼
 ┌──────────── PortfolioManager (scheduler, supervision, reports, control API) ────────────┐
 │  CryptoGridBot      StockTrendBot       ArbitrageBot (scan-only, нет доступа к ордерам)  │
 │      │ OrderGateway (привязан к своей стратегии)                                          │
 │      ▼                                                                                    │
 │  ExecutionEngine ──► RiskManager (проверка ПЕРЕД КАЖДЫМ ордером) ──► CapitalAllocator    │
 │      │  write-ahead в SQLite, идемпотентная обработка исполнений, сверка (reconcile)      │
 │      ▼                                                                                    │
 │  Venues: PaperExchange (PAPER)  |  CCXTExchange / AlpacaBroker (LIVE)                     │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

Ключевые принципы:
* **Единственный путь к бирже** — `core/execution_engine.py`. Боты получают только
  `ReadOnlyMarketData` (рыночные данные) и `OrderGateway`, привязанный к их `strategy_id`.
* **Каждый ордер**: дубликат/конфликт → округление по точности биржи → `RiskManager.check_order`
  → резервирование капитала → запись `PENDING_SUBMIT` в БД → отправка → обработка исполнений
  (исполнение + позиция + ордер в **одной транзакции**, идемпотентно).
* При тайм-ауте отправки ордер **не отправляется повторно вслепую** — сначала поиск по `client_order_id`.
* Состояние рисков (блокировки, пик капитала, дневной старт) хранится в БД и переживает перезапуск.

Структура каталогов:

```
trinity/
  main.py                 точка входа (всегда PAPER)
  config/                 settings.py (env + валидация), strategies.yaml, instruments.yaml
  core/                   portfolio_manager, capital_allocator, market_regime, scheduler,
                          execution_engine, position_manager, order_gateway, live_guard,
                          market_calendar, indicators, models, app (сборка), log
  bots/                   crypto_grid/ stock_trend/ arbitrage/  (bot.py, strategy/scanner, signals/simulator)
  risk/                   risk_manager, position_sizing, drawdown_monitor, circuit_breaker, exposure_manager
  exchanges/              base_exchange, crypto_exchange (CCXT), stock_broker (Alpaca), paper_exchange,
                          simulated_market (СИНТЕТИКА для тестов/симуляции)
  ai/                     market_analyzer, anomaly_detector, performance_analyzer
  notifications/          telegram_bot, message_templates, notifier
  database/               models, repository, migrations/001_initial.sql
  backtesting/            engine, metrics, walk_forward, strategies (адаптеры тех же стратегий)
  monitoring/             health, watchdog, system_metrics, process_lock
  dashboard/app.py        Streamlit
  scripts/                install.sh, backup.py, run_paper.py, run_live.py, emergency_stop.py,
                          run_backtest.py, simulate_24h.py, windows/*.ps1|*.bat
  tests/                  pytest (139 тестов)
```

## 3. Установка

Требуется Python **3.11 или 3.12** (рекомендуется 3.12).

```bash
# Linux / macOS / WSL
bash trinity/scripts/install.sh
```

```powershell
# Windows 10/11 (PowerShell, из корня репозитория)
powershell -ExecutionPolicy Bypass -File trinity\scripts\windows\install.ps1
```

Вручную:

```bash
python -m venv trinity/.venv
source trinity/.venv/bin/activate          # Windows: trinity\.venv\Scripts\activate
pip install -r trinity/requirements.txt
cp trinity/.env.example trinity/.env        # Windows: copy trinity\.env.example trinity\.env
python trinity/main.py --init-db            # создать/мигрировать SQLite
python trinity/main.py --check              # проверить конфигурацию
cd trinity && python -m pytest -q           # тесты
```

Docker (только PAPER):

```bash
cp trinity/.env.example trinity/.env
docker compose -f trinity/docker-compose.yml up -d --build
```
Панель будет на http://127.0.0.1:8501. *Docker-конфигурация в среде разработки не собиралась (Docker недоступен) — проверьте сборку у себя.*

## 4. Конфигурация

* `trinity/.env` — режимы, секреты, биржи (см. `.env.example`). Секреты — **только** здесь.
* `config/strategies.yaml` — капитал, лимиты риска, параметры стратегий, paper-симуляция.
* `config/instruments.yaml` — инструменты (шаг лота, минимумы, комиссии-оценки, группы корреляции),
  торговая сессия США и праздники (сверяйте с официальным календарём NYSE).

Все значения валидируются при запуске (pydantic). Например, `leverage != 0`, `allow_margin: true`,
`max_risk_per_trade_pct > 1%` или сумма аллокаций+резерв > капитала — **ошибка запуска**.

Пример изменения размера уровня сетки (`strategies.yaml`):

```yaml
strategies:
  crypto_grid:
    grid:
      levels: 3
      level_notional_usd: 20        # фиксированный размер, никогда не растёт после убытков
      max_inventory_usd_per_symbol: 60
```

Валюта: внутренний расчёт — USD. USDT считается 1:1 к USD (`currency.usdt_usd_rate`).
Для счёта в EUR: `ACCOUNT_CURRENCY=EUR` и `EUR_USD_RATE=...` в `.env` (курс задаёт пользователь;
автоматическая загрузка курса не реализована — см. ограничения).

## 5. Подключение API-ключей

Подробно — [API_SETUP.md](API_SETUP.md). Кратко:
* Для **PAPER** ключи бирж **не нужны**: используются публичные рыночные данные CCXT.
  Для акций нужен источник данных — бесплатный ключ Alpaca (paper-аккаунт) или CSV-файлы.
* Ключи: только права на **торговлю**, **вывод средств отключён**, по возможности — привязка к IP.
* Никогда не вводите seed-фразы и приватные ключи кошельков — TRINITY их не запрашивает.

## 6. Настройка Telegram

1. В Telegram откройте `@BotFather` → `/newbot` → получите токен.
2. Узнайте свой числовой user id (например, через `@userinfobot`).
3. В `.env`: `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_ALLOWED_USER_IDS=123456789`.
   Без списка ID Telegram **отключается** (защита).

Команды: `/start /status /balance /pnl /positions /trades /grid /stocks /arbitrage /risk /report
/pause /resume /stop /emergency_stop /paper /live_status`.

* Посторонние пользователи получают «Access denied», попытка пишется в журнал.
* `/emergency_stop` сначала блокирует все новые ордера, затем отменяет открытые. Позиции не ликвидируются.
* `/resume` снимает **только** паузу. Блокировки по просадке, аварийной остановке и требование
  подтверждения реальной торговли снимаются только локально.
* Telegram не может включить LIVE.
* Уведомления: открытие/закрытие позиций, стоп-лосс, тейк-профит, дневной отчёт, ошибки API,
  лимиты риска, остановка бота, предупреждение о просадке, расхождения при сверке.

## 7. Виртуальная торговля (PAPER)

```bash
python trinity/scripts/run_paper.py        # или trinity\scripts\windows\start_paper.bat
```

Виртуальная биржа (`exchanges/paper_exchange.py`) моделирует: рыночные ордера по стакану
(+проскальзывание), лимитные ордера с исполнением только при пересечении цены, частичные исполнения,
комиссии maker/taker, недостаточный баланс, отклонение (минимумы, закрытый рынок, дубликат id).
Состояние виртуальной биржи сохраняется в БД и переживает перезапуск.

Минимальные критерии перед рассмотрением LIVE (проверяются автоматически, `/paper` показывает прогресс):
≥ 30 дней работы, ≥ 100 закрытых сделок, 0 критических ошибок исполнения, положительный чистый
результат. **Это минимальные критерии проверки, а не гарантия доходности.** Если сделок мало — продолжайте тестирование.

## 8. Бэктестинг

```bash
# загрузить историю (нужен доступ к бирже из вашей сети)
python trinity/scripts/run_backtest.py --download binance --symbol BTC/USDT --timeframe 15m --bars 5000
# train/validation/test (хронологически), итоговая оценка один раз на test
python trinity/scripts/run_backtest.py --strategy grid --csv trinity/data/csv/BTC_USDT_15m.csv --symbol BTC/USDT
# walk-forward
python trinity/scripts/run_backtest.py --strategy trend --csv trinity/data/csv/SPY.csv --symbol SPY --walk-forward
```

Движок (`backtesting/engine.py`): решение на закрытом баре *t* исполняется не раньше бара *t+1*;
лимиты исполняются только при «проторговке» сквозь цену; объём исполнения ограничен долей объёма бара
(частичные исполнения); спред, проскальзывание, комиссии; стоп-ордера с гэп-риском; сравнение с
buy&hold. Метрики: чистая доходность, % прибыльных, Profit Factor, максимальная просадка, Sharpe,
Sortino, средняя сделка, число сделок, комиссии. Бэктест использует **тот же код стратегий**, что и боты.

Смещение выживших: для ETF SPY/QQQ/VTI оно минимально (индексные фонды существуют весь период),
но при добавлении отдельных акций нужна история делистингованных бумаг — встроенного источника нет.

## 9. Панель управления

```bash
streamlit run trinity/dashboard/app.py      # или start_dashboard.bat → http://127.0.0.1:8501
```
Страницы: Overview, Crypto Grid, Stock Trend, Arbitrage, Risk, System. Переключатель PAPER/LIVE —
данные режимов **никогда не смешиваются**; в PAPER постоянно виден баннер «VIRTUAL».
Статусы: `PAPER MODE`, `LIVE MODE`, `STOPPED`, `PAUSED`, `RISK LOCKED`, `CONNECTION ERROR`,
`SCAN ONLY (VIRTUAL)`, `NO DATA SOURCE`. Панель открывает БД только на чтение.

## 10. Включение реальной торговли

Только через `python trinity/scripts/run_live.py` (или `start_live.bat`). Каждый запуск проверяет:

1. `LIVE_TRADING=true` **и** `PAPER_TRADING=false` в `.env` (меняете сами);
2. `LIVE_TRADING_ACKNOWLEDGEMENT` = `I UNDERSTAND THAT LIVE TRADING CAN LOSE REAL MONEY`;
3. наличие ключей (и `ALPACA_PAPER_ACCOUNT=false`, если включён Stock Trend);
4. полный прогон тестов **прошёл** на **этой же версии кода** (хеш) не старше 7 дней;
5. корректность лимитов риска;
6. биржа/брокер: загрузка рынков, наличие инструментов, работа ключа, кэш-счёт без маржи;
7. критерии paper-валидации (раздел 7);
8. нет аварийной остановки / блокировки по просадке;
9. интерактивное подтверждение в консоли: ввод `START LIVE TRADING`.

После старта выполняется сверка балансов, ордеров и позиций; при ошибке LIVE блокируется.
`main.py`, автозапуск и Docker **всегда** стартуют в PAPER — после перезагрузки LIVE не возобновляется.
Арбитраж в LIVE не торгует никогда (реальное исполнение не реализовано намеренно).

## 11. Настройки рисков

См. [RISK_POLICY.md](RISK_POLICY.md). По умолчанию: плечо 0; риск на сделку ≤ 1% капитала стратегии
($1.50); дневной убыток 2% начального капитала ($10) → блок новых входов до следующих суток UTC;
просадка 8% от пика → блок, отмена ордеров, отключение LIVE, ручное подтверждение; общая экспозиция
≤ 60% ($300) вместе с ожидающими ордерами; группа корреляции ≤ 30% ($150); ордер ≤ $75; ≤ 6 позиций.

## 12. Устранение неисправностей

| Симптом | Причина / решение |
|---|---|
| `CONNECTION ERROR` у ботов | нет доступа к бирже (интернет, блокировка, регион). Логи: `trinity/logs/trinity.jsonl` |
| `NO DATA SOURCE` / stock_trend не запущен | нет ключей Alpaca или `STOCK_DATA_PROVIDER=none` |
| arbitrage не запущен | в `ARB_EXCHANGE_IDS` меньше 2 бирж |
| `RISK LOCKED` | `/risk` или `python trinity/scripts/emergency_stop.py --status` |
| `recovery_lock` | сверка при старте не прошла / БД восстановлена. Проверьте биржу, перезапустите |
| «another TRINITY engine holds …» | уже запущен второй экземпляр (защита блокировкой процесса) |
| Telegram молчит | пустой `TELEGRAM_ALLOWED_USER_IDS`, неверный токен, нет доступа к api.telegram.org |
| LIVE не включается | `python trinity/scripts/run_live.py --check` покажет непройденные проверки |

## 13. Резервное копирование и восстановление

* Автоматически: каждые 6 часов из работающего движка + ежедневная задача Windows (`register_autostart.ps1`).
  Хранятся последние 14 копий в `trinity/data/backups/`.
* Вручную: `python trinity/scripts/backup.py`, список: `--list`.
* Восстановление (движок остановлен): `python trinity/scripts/backup.py --restore trinity/data/backups/trinity-....db`.
* При старте выполняется `PRAGMA integrity_check`. Повреждённая БД переносится в `*.corrupt-*`,
  восстанавливается последняя копия, торговля блокируется до успешной сверки с биржей.
* SQLite работает в режиме WAL + `synchronous=FULL`: подтверждённые транзакции переживают отключение питания.

## 14. Безопасность

* Секреты только в `.env`/переменных окружения (`SecretStr`), не в БД, не в YAML, не в логах:
  фильтр маскирует зарегистрированные секреты, токены Telegram и `key=value`-паттерны; логгер
  `httpx` приглушён (URL Telegram содержит токен).
* Ключи бирж — только торговля, вывод запрещён. Адаптеры не содержат функций вывода/переводов.
* Telegram — только авторизованные user id. Панель слушает только `127.0.0.1`.
* `.env` исключён из git; установщик Windows ограничивает права на файл текущим пользователем.

## 15. Известные ограничения

* **Реальные подключения не проверены**: в среде разработки исходящие запросы к биржам/брокерам
  блокировались сетевой политикой (HTTP 403 прокси), ключей не было. CCXT- и Alpaca-адаптеры
  протестированы только на имитации ответов. Проверьте их на своём paper-аккаунте.
* Arbitrage — только сканирование/виртуальная торговля; модель не учитывает риск «одной ноги».
* Стопы для акций синтетические (бот проверяет их раз в минуту в сессию); между сессиями возможен гэп
  ниже стопа. Нативные стоп-ордера брокера не используются.
* Комиссии и минимумы в `instruments.yaml` — оценки; в LIVE минимумы берутся с биржи, комиссии
  оцениваются по taker-ставке, если биржа их не вернула. Комиссия в BNB/иной валюте оценивается.
* USDT=USD 1:1 (риск депега). Курс EUR/USD задаётся вручную.
* Календарь праздников NYSE в конфиге нужно ежегодно сверять (приоритет у часов брокера).
* Внешний LLM не требуется и не подключён; «AI»-модули детерминированные. Рекомендации никогда не применяются автоматически.
* Метрики 24-часовой имитации и бэктестов на синтетике не являются оценкой доходности.
* Docker-образ и Windows-скрипты в среде разработки не запускались (Linux без Docker/Windows).
