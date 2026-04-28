# Agent Pro — Chat

Браузерное приложение Agent Pro плюс набор локальных сервисов, которые ему нужны:

| Компонент | Что делает | Адрес по умолчанию |
| --- | --- | --- |
| **Frontend** (`index.html`) | Сам чат-интерфейс, работает в браузере | <http://localhost:8080> |
| **Workspace API** (`wsapi_server.py`, Flask) | Файловая система агента (`/ws/list`, `/ws/read`, `/ws/write`, …) | <http://localhost:8764> |
| **Terminal Server** (`server.js`, Node + node-pty) | Удалённый PTY и одноразовые команды для агента | `ws://localhost:8765/term`, `ws://localhost:8765/exec` |
| **MCP stdio Bridge** (`bridge/agent-pro-bridge.mjs`) | Локальный мост, чтобы браузер мог запускать stdio-MCP-серверы | `ws://127.0.0.1:7777` |
| **agent-browser** (CLI, `tools/agent-browser-termux/`) | Persistent Playwright-Chromium для `browser_action` в чате | `~/.local/bin/agent-browser` (или `$PREFIX/bin` на Termux) |

Раньше эти процессы нужно было запускать руками в разных терминалах, плюс отдельно
ставить agent-browser shim. Теперь после клона репозитория всё стартует **одной
командой**.

---

## Быстрый старт (одна команда)

Требования:

* **Node.js ≥ 18** (для `node-pty`, `playwright-core`).
* **Python ≥ 3.9** с модулями `venv` и `ensurepip` (на Ubuntu/Debian — пакеты
  `python3-venv` и `python3-pip`; на Termux — `python`).
* **npm** (идёт в комплекте с Node.js).
* **Chromium** или **Google Chrome** в `$PATH` (нужен для `agent-browser`; на Ubuntu —
  `sudo apt install chromium-browser`, на Termux — `pkg install chromium-browser`).
  Без него все остальные сервисы поднимутся, но `browser_action` в чате будет падать.

```bash
git clone https://github.com/RamadanIU/Chat.git
cd Chat
bash start.sh
```

Можно также запустить через npm — это просто алиас:

```bash
npm start
```

Скрипт `start.sh`:

1. Определит окружение (Termux или обычный Linux/macOS) и проверит `node`, `npm`,
   `python3`, версию Node ≥ 18, наличие `python3-venv`/`ensurepip`. На любую
   проблему — печатает понятное сообщение с командой для исправления.
2. Создаст `.venv/` и поставит `flask`, `flask-cors` (для Workspace API).
3. Поставит Node-зависимости в корне (`ws`, `node-pty`) и в `bridge/`. Если
   `node-pty` не собрался — подсказывает, какой `build-essential` поставить.
4. Установит **agent-browser** (`tools/agent-browser-termux/install.sh`):
   создаст `~/playwright-termux/`, поставит туда `playwright-core`, разложит
   `daemon.js` / `cli.js` и положит обёртку `agent-browser` в `~/.local/bin`.
   Этот шаг можно отключить флагом `--no-browser`.
5. Запустит **все четыре сервиса** через оркестратор `run.py`.

### Флаги

* `bash start.sh --no-browser` — пропустить установку agent-browser shim
  (если в чате не нужен `browser_action`).
* `bash start.sh --skip-deps` — не ставить ничего, только запустить (после
  первой успешной установки).
* `bash start.sh --help` — показать встроенную справку.

После запуска откройте в браузере <http://localhost:8080> — это и есть UI чата.
Завершить всё — `Ctrl+C` в том же терминале.

> **Закрытие окна терминала не убивает сервисы** даже в foreground-режиме:
> `run.py` игнорирует `SIGHUP`, а дочерние сервисы (`workspace`, `terminal`,
> `bridge`, `frontend`) живут в отдельных process group'ах через `setsid` и
> tty-сигналы не получают. Чтобы корректно остановить стек после закрытия окна,
> используйте `bash start.sh stop` (если запускали как daemon) или
> `bash start.sh cleanup` / `kill <pid run.py>`.

### Логин и пароль (HTTP Basic Auth)

Frontend `http://localhost:8080` защищён HTTP Basic Auth. Дефолтные креды:

| | |
| --- | --- |
| **Login** | `Ramadan` |
| **Password** | `Bismillah2021` |

Переопределить можно через переменные окружения перед запуском:

```bash
AUTH_USER=alice AUTH_PASSWORD=s3cret bash start.sh
```

Полностью отключить:

```bash
AUTH_DISABLE=1 bash start.sh
```

Браузер один раз спросит креды и закеширует их в рамках сессии.

### Daemon-режим (запуск переживает закрытие терминала)

Если хочется, чтобы все сервисы продолжили работать **после закрытия окна
терминала**, используйте подкоманды:

```bash
bash start.sh start     # фоновый запуск (отвязанный от tty); пишет лог в ~/.cache/chat-stack/daemon.log
bash start.sh status    # показать состояние и адреса
bash start.sh logs      # tail -f лога
bash start.sh stop      # корректно остановить (SIGTERM → дерево процессов)
bash start.sh restart   # stop + start
bash start.sh doctor    # диагностика: кто держит порты + state-файл
bash start.sh cleanup   # освободить порты, прибить сирот предыдущего запуска
```

### Самовосстановление (supervisor + watchdog)

Запуск устроен как двухуровневый supervisor, чтобы упавший сервис не клал весь
стек и порты не оставались занятыми:

* **`run.py`** перезапускает каждый из четырёх сервисов (frontend, workspace,
  terminal, bridge) индивидуально с экспоненциальным backoff
  (0.5s → 1 → 2 → 5 → 10 → 30 → 60). Падение одного сервиса больше **не валит
  остальные**. Перед стартом каждого освобождается его TCP-порт (через
  `lsof`/`ss`/`fuser`), даже если его держит сирота из прошлой сессии.
* **`start.sh start`** оборачивает `run.py` во внешний bash-watchdog: если сам
  `run.py` падает с ненулевым кодом (например, unhandled exception), watchdog
  поднимает его заново. Перед каждым стартом дополнительно вызывается
  `run.py --cleanup-only`.
* **State-файл** `~/.cache/chat-stack/children.json` хранит PID/PGID всех
  запущенных детей. На старте `start.sh` сначала добивает любых живых сирот из
  этого файла, чтобы порты не оставались занятыми после аварии или
  `kill -9` родителя.
* **`bash start.sh stop`** убивает всю process group watchdog-а и затем ещё раз
  гонит cleanup, чтобы гарантированно освободить порты.

Если что-то всё равно «прилипло» — `bash start.sh doctor` покажет, кто
сейчас слушает наши порты, а `bash start.sh cleanup` уберёт остатки.

Внутри `start` использует `setsid + nohup`, поэтому процесс отвязывается от
сессии терминала и переживает `Ctrl+D` / закрытие окна. PID хранится в
`~/.cache/chat-stack/daemon.pid`. Без подкоманды (просто `bash start.sh`)
сервисы стартуют как раньше — в foreground, и Ctrl+C их останавливает.

### Системные пакеты, если что-то упало

| Симптом | Решение |
| --- | --- |
| `python3 -m venv` падает с `ensurepip is not available` | `sudo apt install python3-venv python3-pip` |
| `npm install` падает на `node-pty` (`gyp ERR!`) | `sudo apt install build-essential python3 make g++` (Termux: `pkg install build-essential python make`) |
| `agent-browser open …` → `Could not load playwright-core` | Запустите `bash start.sh` ещё раз без `--no-browser`, либо вручную `bash tools/agent-browser-termux/install.sh`. |
| `agent-browser open …` → `Failed to launch chromium` | Поставьте Chromium: Ubuntu — `sudo apt install chromium-browser`, Termux — `pkg install chromium-browser`. |
| Termux: всё стало, но Chromium не запускается | Прочтите [`tools/agent-browser-termux/README.md`](tools/agent-browser-termux/README.md) — там описаны флаги `AGENT_BROWSER_SINGLE_PROCESS`, `AGENT_BROWSER_HEADLESS`. |

### Что увидите в логе

```
[system   ] ────────────────────────────────────────────────────────────────
[system   ] Agent Pro — единый запуск (run.py)
[system   ] ────────────────────────────────────────────────────────────────
[system   ] Frontend       : http://localhost:8080
[system   ] Workspace API  : http://localhost:8764/ws/ping
[system   ] Terminal (ws)  : ws://localhost:8765/term  | /exec
[system   ] MCP bridge (ws): ws://127.0.0.1:7777
[system   ] ────────────────────────────────────────────────────────────────
```

### Настройка чата

При первом запуске откройте **Settings** в UI и подставьте локальные адреса
из лога:

* **Терминал** → URL сервера: `ws://localhost:8765`
* **Workspace API** → URL сервера: `http://localhost:8764/ws`
* **MCP-серверы (stdio)** — мост уже на `ws://127.0.0.1:7777`, индикатор
  «Локальный мост (stdio): подключён» загорится автоматически.

---

## Конфигурация портов и хоста

Все порты конфигурируются переменными окружения. Меняем — перезапускаем `start.sh`:

| Переменная | По умолчанию | Что меняет |
| --- | --- | --- |
| `FRONTEND_PORT` | `8080` | Порт встроенного HTTP-сервера для `index.html`. |
| `WORKSPACE_PORT` | `8764` | Порт `wsapi_server.py`. |
| `TERM_PORT` | `8765` | Порт `server.js` (PTY/exec). |
| `BRIDGE_PORT` | `7777` | Порт MCP stdio-моста. |
| `HOST` | `0.0.0.0` | Адрес прослушивания для frontend / workspace / terminal. |
| `AGENT_PRO_BRIDGE_HOST` | `127.0.0.1` | Адрес моста (рекомендуется не менять — это локальный security-периметр). |
| `WORKSPACE_DIR` | `~/workspace` (или `~/storage/shared/workspace` на Termux) | Корневая папка файлового API. |
| `WORKSPACE_ROOTS` | `$HOME` | `:`-разделённый список разрешённых корней для смены рабочей области через UI. |
| `TOKEN` | _пусто_ | Если задан — терминал-сервер требует `?token=...` в WebSocket-URL. |
| `AUTH_USER` | `Ramadan` | Логин HTTP Basic Auth на frontend. |
| `AUTH_PASSWORD` | `Bismillah2021` | Пароль HTTP Basic Auth на frontend. |
| `AUTH_DISABLE` | _не задан_ | `1` — полностью отключить авторизацию. |

Пример с другими портами:

```bash
FRONTEND_PORT=9000 TERM_PORT=9876 bash start.sh
```

---

## Запуск отдельных сервисов

Удобно для отладки. Все четыре нужны вместе, но можно запустить любой по отдельности:

```bash
# 1. Workspace API
.venv/bin/python wsapi_server.py --port 8764

# 2. Terminal server
node server.js                       # PORT=8765 по умолчанию

# 3. MCP bridge
( cd bridge && node agent-pro-bridge.mjs )

# 4. Frontend (любой статический сервер)
python3 -m http.server 8080

# 5. agent-browser shim
bash tools/agent-browser-termux/install.sh   # ставит CLI и playwright-core
agent-browser version                        # проверка
agent-browser kill                           # остановить демон
```

---

## Структура репозитория

```
.
├── index.html              # сам чат (браузерное приложение)
├── server.js               # Terminal server (Node)
├── wsapi_server.py         # Workspace API (Flask)
├── bridge/                 # MCP stdio bridge (Node)
│   ├── agent-pro-bridge.mjs
│   └── README.md
├── tools/agent-browser-termux/  # CLI-шим agent-browser (Playwright + Chromium)
├── run.py                  # оркестратор всех сервисов
├── start.sh                # установка зависимостей + run.py
├── requirements.txt        # Python-зависимости
└── package.json            # Node-зависимости + npm start
```

---

## Лицензия

MIT.
