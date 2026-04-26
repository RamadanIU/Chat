#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — единая точка входа: установка зависимостей + запуск всех сервисов.
#
# После `git clone` и `bash start.sh` вы получаете:
#   • Frontend (index.html)        http://localhost:8080
#   • Workspace API                http://localhost:8764/ws/ping
#   • Terminal Server (WebSocket)  ws://localhost:8765/term  |  /exec
#   • MCP stdio Bridge             ws://127.0.0.1:7777
#
# Поддерживаемые переменные окружения:
#   FRONTEND_PORT, WORKSPACE_PORT, TERM_PORT, BRIDGE_PORT, HOST,
#   TOKEN (для terminal server), WORKSPACE_DIR, AGENT_PRO_BRIDGE_HOST.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
err()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; }

bold "Agent Pro — установка и запуск (start.sh)"

# ── Проверки окружения ──────────────────────────────────────────────────────
need() {
  command -v "$1" >/dev/null 2>&1 || { err "не найден '$1' — установите его и повторите."; exit 1; }
}
need node
need npm

# Подбираем python3 (на Termux может быть только `python`)
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  err "не найден python3 — установите Python 3.9+."
  exit 1
fi

NODE_MAJOR=$("$(command -v node)" -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')
if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
  warn "Node.js $(node -v) — рекомендуется 18+."
fi

# ── Python venv + flask/flask-cors ───────────────────────────────────────────
if [ ! -d .venv ]; then
  info "создаю виртуальное окружение .venv (для wsapi_server.py)…"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

if [ ! -f .venv/.deps-installed ] || [ requirements.txt -nt .venv/.deps-installed ]; then
  info "ставлю Python-зависимости (flask, flask-cors)…"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  date > .venv/.deps-installed
  ok "python deps OK"
else
  ok "python deps уже установлены"
fi

# ── Node-зависимости (root) ──────────────────────────────────────────────────
if [ ! -d node_modules ]; then
  info "ставлю Node-зависимости в корне (ws, node-pty)…"
  npm install --omit=dev --no-audit --no-fund
  ok "root node_modules OK"
else
  ok "root node_modules уже установлены"
fi

# ── Node-зависимости (bridge) ────────────────────────────────────────────────
if [ ! -d bridge/node_modules ]; then
  info "ставлю Node-зависимости в bridge/ …"
  ( cd bridge && npm install --omit=dev --no-audit --no-fund )
  ok "bridge node_modules OK"
else
  ok "bridge node_modules уже установлены"
fi

# ── Запуск ───────────────────────────────────────────────────────────────────
echo
bold "запускаю все сервисы (Ctrl+C — остановить):"
exec python run.py "$@"
