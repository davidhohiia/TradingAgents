# TRINITY 3.0 — быстрый старт (PAPER, Windows 10/11)

1. Установите Python 3.12 с https://www.python.org/downloads/ (отметьте «Add python.exe to PATH»).
2. Скачайте/клонируйте репозиторий, откройте PowerShell в его корне.
3. Установка:
   ```powershell
   powershell -ExecutionPolicy Bypass -File trinity\scripts\windows\install.ps1
   ```
   Скрипт создаст `trinity\.venv`, установит зависимости, создаст `trinity\.env`, инициализирует БД и прогонит тесты.
4. Откройте `trinity\.env` в Блокноте. Для PAPER достаточно оставить значения по умолчанию.
   * Акции в PAPER: зарегистрируйте бесплатный paper-аккаунт Alpaca и впишите `ALPACA_API_KEY_ID`,
     `ALPACA_API_SECRET_KEY` (оставьте `ALPACA_PAPER_ACCOUNT=true`). Без них Stock Trend не запустится.
   * Telegram (по желанию): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`.
5. Запуск виртуальной торговли: двойной клик `trinity\scripts\windows\start_paper.bat`.
6. Панель: `trinity\scripts\windows\start_dashboard.bat` → http://127.0.0.1:8501
7. Автозапуск PAPER при входе в Windows + ежедневный бэкап:
   ```powershell
   powershell -ExecutionPolicy Bypass -File trinity\scripts\windows\register_autostart.ps1
   ```
8. Ежемесячное пополнение (попадает в резерв, не считается доходом):
   `trinity\.venv\Scripts\python.exe trinity\scripts\capital.py --deposit 200`
   Перераспределение из резерва — только `capital.py --propose` и `--approve ID` с подтверждением.
9. Аварийная остановка: `/emergency_stop` в Telegram или `trinity\scripts\windows\emergency_stop.bat`.

Linux/macOS: `bash trinity/scripts/install.sh`, затем `python trinity/scripts/run_paper.py`.

Проверка без запуска торговли: `python trinity\main.py --check`.
LIVE: только после выполнения всех условий из README §10 — `start_live.bat`.
