# Agent Pro — Chat

Браузерное приложение Agent Pro плюс набор локальных сервисов, которые ему нужны:

| Компонент | Что делает | Адрес по умолчанию |
| --- | --- | --- |
| **Frontend** (`index.html`) | Сам чат-интерфейс, работает в браузере | <http://localhost:8080> |
| **Workspace API** (`wsapi_server.py`, Flask) | Файловая система агента (`/ws/list`, `/ws/read`, `/ws/write`, …) | <http://localhost:8764> |
| **Terminal Server** (`server.js`, Node + node-pty) | Удалённый PTY и одноразовые команды для агента | `ws://localhost:8765/term`, `ws://localhost:8765/exec` |
| **MCP stdio Bridge** (`bridge/agent-pro-bridge.mjs`) | Локальный мост, чтобы браузер мог запускать stdio-MCP-серверы | `ws://127.0.0.1:7777` |

Раньше эти четыре процесса нужно было запускать руками в разных терминалах. Теперь
после клона репозитория всё стартует **одной командой**.

---

## Быстрый старт (одна команда)

Требования: **Node.js ≥ 18**, **Python ≥ 3.9**, `npm`, `pip`.

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

1. Проверит наличие `node`, `npm`, `python3`.
2. Создаст `.venv/` и поставит `flask`, `flask-cors` (для Workspace API).
3. Поставит Node-зависимости в корне (`ws`, `node-pty`) и в `bridge/`.
4. Запустит **все четыре сервиса** через оркестратор `run.py`.

После запуска откройте в браузере <http://localhost:8080> — это и есть UI чата.
Завершить всё — `Ctrl+C` в том же терминале.

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
├── tools/agent-browser-termux/  # CLI-шим agent-browser для Termux
├── run.py                  # оркестратор всех сервисов
├── start.sh                # установка зависимостей + run.py
├── requirements.txt        # Python-зависимости
└── package.json            # Node-зависимости + npm start
```

---

## Лицензия

MIT.
