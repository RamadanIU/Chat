#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — единая точка входа: установка, запуск, daemon-режим.
#
# Команды:
#   bash start.sh                  — поставить зависимости и запустить foreground
#                                    (Ctrl+C останавливает всё, как раньше).
#   bash start.sh start            — фоновый daemon: переживает закрытие терминала.
#   bash start.sh stop             — остановить daemon.
#   bash start.sh status           — показать состояние daemon-а и адреса сервисов.
#   bash start.sh restart          — stop + start.
#   bash start.sh logs             — tail -f лога daemon-а.
#   bash start.sh run              — то же что без аргументов (foreground).
#   bash start.sh doctor           — диагностика: кто держит порты + state-файл.
#   bash start.sh cleanup          — освободить порты, прибить сирот предыдущего запуска.
#
# Флаги (для foreground / start):
#   --no-browser     не ставить agent-browser shim.
#   --no-browser-act не ставить BrowserAct CLI.
#   --skip-deps      пропустить установку зависимостей (только запуск).
#
# Что получаем после запуска:
#   • Frontend (index.html)        http://localhost:8080  (HTTP Basic Auth)
#   • Workspace API                http://localhost:8764/ws/ping
#   • Terminal Server (WebSocket)  ws://localhost:8765/term  |  /exec
#   • MCP stdio Bridge             ws://127.0.0.1:7777
#   • CLI agent-browser            (для browser_action в чате)
#   • CLI browser-act              (BrowserAct: stealth/real Chrome/captcha/network)
#
# Логин/пароль на frontend (HTTP Basic Auth):
#   AUTH_USER       (default Ramadan)
#   AUTH_PASSWORD   (default Bismillah2021)
#   Чтобы отключить — установите AUTH_DISABLE=1.
#
# Прочие переменные окружения:
#   FRONTEND_PORT, WORKSPACE_PORT, TERM_PORT, BRIDGE_PORT, HOST,
#   TOKEN (terminal-server), WORKSPACE_DIR, AGENT_PRO_BRIDGE_HOST,
#   PLAYWRIGHT_TERMUX_ROOT, AGENT_BROWSER_PORT,
#   AGENT_PRO_BROWSERACT_AUTO_UPGRADE (default 1).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
err()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; }

# ── Daemon paths ─────────────────────────────────────────────────────────────
RUN_DIR="${HOME}/.cache/chat-stack"
PID_FILE="${RUN_DIR}/daemon.pid"
LOG_FILE="${RUN_DIR}/daemon.log"
mkdir -p "${RUN_DIR}"

# ── Парсинг команды и флагов ─────────────────────────────────────────────────
CMD="run"
WITH_BROWSER=1
WITH_BROWSER_ACT=1
SKIP_DEPS=0
PASSTHROUGH=()

if [ $# -gt 0 ]; then
  case "$1" in
    start|stop|status|restart|logs|run|doctor|cleanup) CMD="$1"; shift ;;
  esac
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --no-browser)     WITH_BROWSER=0;     shift ;;
    --no-browser-act) WITH_BROWSER_ACT=0; shift ;;
    --skip-deps)      SKIP_DEPS=1;        shift ;;
    --help|-h)
      sed -n '2,38p' "$0"
      exit 0
      ;;
    *) PASSTHROUGH+=("$1"); shift ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid; pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# Освободить порты и прибить «сирот» предыдущего запуска (best-effort).
# Делегируем в run.py --cleanup-only — он умеет читать state-файл, валить
# по PGID, и через lsof/ss/fuser освобождать порты.
cleanup_stale() {
  prepare_runtime_env >/dev/null 2>&1 || true
  local PYBIN
  if [ -x ".venv/bin/python" ]; then
    PYBIN=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYBIN="python3"
  else
    PYBIN="python"
  fi
  "$PYBIN" "${ROOT}/run.py" --cleanup-only 2>&1 \
    | sed 's/^/[cleanup] /' || true
}

# Watchdog: в daemon-режиме перезапускает run.py при ненулевом коде выхода
# (не чаще 5 раз за минуту, чтобы при сломанной конфигурации не крутиться вечно).
print_watchdog_script() {
  cat <<'WATCHDOG_EOF'
#!/usr/bin/env bash
set -u
cd "${AGENT_PRO_ROOT}"
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi
attempts=0
window_start=$(date +%s)
echo "[watchdog] started pid=$$ root=${AGENT_PRO_ROOT}"
while true; do
  python "${AGENT_PRO_ROOT}/run.py" --cleanup-only || true
  python "${AGENT_PRO_ROOT}/run.py" "$@"
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "[watchdog] run.py вышел штатно (rc=0), завершаю watchdog."
    break
  fi
  now=$(date +%s)
  if [ $((now - window_start)) -ge 60 ]; then
    window_start=$now
    attempts=0
  fi
  attempts=$((attempts + 1))
  delay=2
  if [ $attempts -ge 3 ]; then delay=5; fi
  if [ $attempts -ge 5 ]; then delay=15; fi
  if [ $attempts -ge 8 ]; then delay=60; fi
  echo "[watchdog] run.py упал rc=$rc (попыток за минуту: $attempts). Рестарт через ${delay}s…"
  sleep "$delay"
done
WATCHDOG_EOF
}

# ── Окружение ────────────────────────────────────────────────────────────────
detect_env() {
  if [ -n "${PREFIX:-}" ] && [ -d "/data/data/com.termux" ]; then
    IS_TERMUX=1
    BIN_DIR="${PREFIX}/bin"
  else
    IS_TERMUX=0
    BIN_DIR="${HOME}/.local/bin"
  fi
}

need() {
  command -v "$1" >/dev/null 2>&1 || {
    err "не найден '$1' — установите его и повторите."
    case "$1" in
      node|npm)
        if [ "${IS_TERMUX:-0}" -eq 1 ]; then echo "  Termux: pkg install -y nodejs"
        else echo "  Ubuntu/Debian: sudo apt install -y nodejs npm  # или https://nodejs.org/"
        fi ;;
      python3|python)
        if [ "${IS_TERMUX:-0}" -eq 1 ]; then echo "  Termux: pkg install -y python"
        else echo "  Ubuntu/Debian: sudo apt install -y python3 python3-venv python3-pip"
        fi ;;
    esac
    exit 1
  }
}

install_deps() {
  detect_env
  bold "Agent Pro — установка и запуск (start.sh)"
  info "среда: $([ $IS_TERMUX -eq 1 ] && echo Termux || echo 'Linux/macOS')  (bin: $BIN_DIR)"

  need node
  need npm

  if command -v python3 >/dev/null 2>&1; then PYTHON=python3
  elif command -v python >/dev/null 2>&1;  then PYTHON=python
  else need python3
  fi

  local NODE_MAJOR
  NODE_MAJOR=$("$(command -v node)" -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')
  if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
    err "Node.js $(node -v) — нужен ≥ 18 (требование node-pty 1.x и playwright-core)."
    exit 1
  fi
  ok "node $(node -v) | $PYTHON $($PYTHON -V 2>&1 | awk '{print $2}')"

  if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    err "у '$PYTHON' нет модуля venv/ensurepip."
    [ $IS_TERMUX -eq 0 ] && echo "  Ubuntu/Debian: sudo apt install -y python3-venv python3-pip"
    exit 1
  fi

  if [ $SKIP_DEPS -eq 1 ]; then
    warn "--skip-deps: пропускаю установку зависимостей."
    return 0
  fi

  # Python venv + flask/flask-cors
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

  # Node deps (root)
  if [ ! -d node_modules ] || [ ! -d node_modules/node-pty ] || [ ! -d node_modules/ws ]; then
    info "ставлю Node-зависимости в корне (ws, node-pty)…"
    if ! npm install --omit=dev --no-audit --no-fund; then
      err "npm install в корне упал. Возможно, не хватает build tools для node-pty."
      [ $IS_TERMUX -eq 0 ] && echo "  Ubuntu/Debian: sudo apt install -y build-essential python3 make g++" \
                          || echo "  Termux: pkg install -y build-essential python make"
      exit 1
    fi
    ok "root node_modules OK"
  else
    ok "root node_modules уже установлены"
  fi

  # BrowserAct CLI (stealth/real Chrome/captcha/network browser automation)
  if [ $WITH_BROWSER_ACT -eq 1 ]; then
    local AUTO_UPGRADE
    AUTO_UPGRADE="${AGENT_PRO_BROWSERACT_AUTO_UPGRADE:-1}"
    if command -v uv >/dev/null 2>&1; then
      if ! command -v browser-act >/dev/null 2>&1; then
        info "ставлю BrowserAct CLI (browser-act-cli через uv)…"
        uv tool install browser-act-cli --python 3.12
        ok "BrowserAct CLI установлен"
      elif [ "$AUTO_UPGRADE" != "0" ]; then
        info "обновляю BrowserAct CLI (browser-act-cli через uv)…"
        uv tool upgrade browser-act-cli || true
        ok "BrowserAct CLI готов"
      else
        ok "BrowserAct CLI уже установлен ($(command -v browser-act))"
      fi
    else
      warn "uv не найден — BrowserAct CLI не установлен."
      [ $IS_TERMUX -eq 1 ] && echo "    Termux: pkg install -y uv  # или pip install --user uv" \
                           || echo "    Ubuntu/Debian: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
  else
    warn "--no-browser-act: пропускаю установку BrowserAct CLI."
  fi

  # Node deps (bridge)
  if [ ! -d bridge/node_modules ] || [ ! -d bridge/node_modules/ws ]; then
    info "ставлю Node-зависимости в bridge/ …"
    ( cd bridge && npm install --omit=dev --no-audit --no-fund )
    ok "bridge node_modules OK"
  else
    ok "bridge node_modules уже установлены"
  fi

  # agent-browser shim
  if [ $WITH_BROWSER -eq 1 ]; then
    local PT_ROOT WRAPPER CHROMIUM
    PT_ROOT="${PLAYWRIGHT_TERMUX_ROOT:-${HOME}/playwright-termux}"
    WRAPPER="${BIN_DIR}/agent-browser"

    CHROMIUM=""
    for p in chromium chromium-browser google-chrome google-chrome-stable; do
      command -v "$p" >/dev/null 2>&1 && CHROMIUM="$(command -v "$p")" && break
    done
    if [ -z "$CHROMIUM" ]; then
      for p in /usr/bin/chromium /usr/bin/chromium-browser /usr/bin/google-chrome \
               "${PREFIX:-/usr}/bin/chromium" "${PREFIX:-/usr}/bin/chromium-browser"; do
        [ -x "$p" ] && CHROMIUM="$p" && break
      done
    fi

    if [ ! -x "$WRAPPER" ] || [ ! -d "${PT_ROOT}/node_modules/playwright-core" ]; then
      info "ставлю agent-browser shim (для browser_action в чате)…"
      bash tools/agent-browser-termux/install.sh || true
      if [ -x "$WRAPPER" ] && [ -d "${PT_ROOT}/node_modules/playwright-core" ]; then
        ok "agent-browser shim установлен ($WRAPPER)"
      else
        warn "agent-browser shim не установился — browser_action работать не будет."
        warn "перезапустите с --no-browser, либо см. tools/agent-browser-termux/README.md"
      fi
    else
      ok "agent-browser shim уже установлен ($WRAPPER)"
    fi

    if [ -z "$CHROMIUM" ]; then
      warn "Chromium не найден. agent-browser будет падать при первом open."
      [ $IS_TERMUX -eq 1 ] && echo "    Termux: pkg install -y chromium-browser" \
                           || echo "    Ubuntu/Debian: sudo apt install -y chromium-browser"
    else
      ok "Chromium: $CHROMIUM"
    fi
  else
    warn "--no-browser: agent-browser shim пропущен."
  fi
}

# Активирует venv и кладёт ~/.local/bin в PATH; возвращает path к python.
prepare_runtime_env() {
  if [ -d .venv ] && [ -z "${VIRTUAL_ENV:-}" ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
  fi
  if [ -d "${HOME}/.local/bin" ]; then
    case ":$PATH:" in
      *":${HOME}/.local/bin:"*) ;;
      *) export PATH="${HOME}/.local/bin:$PATH" ;;
    esac
  fi
}

print_status() {
  local FP WP TP BP HOST_DEFAULT
  FP="${FRONTEND_PORT:-8080}"
  WP="${WORKSPACE_PORT:-8764}"
  TP="${TERM_PORT:-8765}"
  BP="${BRIDGE_PORT:-7777}"
  HOST_DEFAULT="${HOST:-0.0.0.0}"
  if is_running; then
    local pid uptime
    pid=$(cat "$PID_FILE")
    if [ -r "/proc/$pid/stat" ]; then
      uptime=$(ps -o etime= -p "$pid" 2>/dev/null | xargs || echo "?")
    else
      uptime="?"
    fi
    ok "Daemon работает (pid=$pid, uptime=$uptime)"
    echo "  Frontend       : http://localhost:${FP}"
    echo "  Workspace API  : http://localhost:${WP}/ws/ping"
    echo "  Terminal (ws)  : ws://localhost:${TP}/term  |  /exec"
    echo "  MCP bridge     : ws://127.0.0.1:${BP}"
    echo "  Лог            : ${LOG_FILE}"
    if [ "${AUTH_DISABLE:-0}" = "1" ]; then
      echo "  Auth (frontend): ОТКЛЮЧЕНА (AUTH_DISABLE=1)"
    else
      echo "  Auth (frontend): ${AUTH_USER:-Ramadan} / ${AUTH_PASSWORD:-Bismillah2021}"
    fi
  else
    warn "Daemon не запущен."
    [ -f "$PID_FILE" ] && echo "  (старый pid-файл $PID_FILE удаляю)" && rm -f "$PID_FILE"
  fi
}

cmd_start() {
  if is_running; then
    ok "Daemon уже работает (pid=$(cat "$PID_FILE")). Используйте restart, чтобы перезапустить."
    return 0
  fi
  install_deps
  prepare_runtime_env
  # Прибить любых сирот предыдущего запуска и освободить порты ДО старта.
  cleanup_stale

  echo
  bold "запускаю фоновый daemon (PID-файл: $PID_FILE, лог: $LOG_FILE)"
  : > "$LOG_FILE"

  # ВНЕШНИЙ watchdog: bash-цикл, который перезапускает run.py если он сам упал.
  # PID в PID_FILE — pid этого watchdog-цикла. setsid + nohup отвязывают его от
  # tty, чтобы он пережил закрытие окна.
  local WATCHDOG_SCRIPT="${RUN_DIR}/watchdog.sh"
  print_watchdog_script > "$WATCHDOG_SCRIPT"
  chmod +x "$WATCHDOG_SCRIPT"

  if command -v setsid >/dev/null 2>&1; then
    AGENT_PRO_ROOT="${ROOT}" setsid nohup bash "$WATCHDOG_SCRIPT" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
      </dev/null >>"$LOG_FILE" 2>&1 &
  else
    AGENT_PRO_ROOT="${ROOT}" nohup bash "$WATCHDOG_SCRIPT" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
      </dev/null >>"$LOG_FILE" 2>&1 &
  fi
  echo $! > "$PID_FILE"
  disown 2>/dev/null || true

  # Подождём, пока сервисы реально стартуют (или упадут)
  local i=0
  while [ $i -lt 30 ]; do
    if ! is_running; then
      err "daemon упал на старте; смотри лог:"
      tail -n 40 "$LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    if grep -q "Frontend       : http" "$LOG_FILE" 2>/dev/null; then break; fi
    sleep 0.3
    i=$((i + 1))
  done

  ok "Daemon запущен (pid=$(cat "$PID_FILE"))."
  print_status
  echo
  echo "  bash start.sh logs    — следить за логом"
  echo "  bash start.sh stop    — остановить"
}

cmd_stop() {
  if ! is_running; then
    warn "Daemon не запущен."
    rm -f "$PID_FILE"
    # Всё равно дочистим — на случай если PID-файл потёрли руками, а дети живы.
    cleanup_stale
    return 0
  fi
  local pid; pid=$(cat "$PID_FILE")
  info "останавливаю daemon (pid=$pid)…"
  # Вначале гасим всю process group watchdog-а (а с ней и дочерний run.py с детьми).
  # Если pid в группе == pid процесса (типичный случай setsid), это покрывает всё.
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  local i=0
  while [ $i -lt 50 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 0.2
    i=$((i + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "не остановился по TERM, шлю KILL"
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  # Финальная подчистка: если кто-то из детей пережил (стал сиротой) — добьём.
  cleanup_stale
  ok "Daemon остановлен."
}

cmd_status() {
  print_status
}

cmd_restart() {
  cmd_stop || true
  cmd_start
}

cmd_logs() {
  if [ ! -f "$LOG_FILE" ]; then
    err "лог $LOG_FILE ещё не создан (daemon не запускался)."
    exit 1
  fi
  exec tail -n 200 -f "$LOG_FILE"
}

cmd_run() {
  install_deps
  prepare_runtime_env
  cleanup_stale
  echo
  bold "запускаю все сервисы (Ctrl+C — остановить):"
  exec python run.py ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
}

cmd_doctor() {
  prepare_runtime_env >/dev/null 2>&1 || true
  bold "doctor — состояние стека"
  print_status
  echo
  echo "── Кто слушает наши порты ──"
  local PORTS="${FRONTEND_PORT:-8080} ${WORKSPACE_PORT:-8764} ${TERM_PORT:-8765} ${BRIDGE_PORT:-7777}"
  for p in $PORTS; do
    echo "port $p:"
    if command -v lsof >/dev/null 2>&1; then
      lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | sed 's/^/  /' || true
    elif command -v ss >/dev/null 2>&1; then
      ss -ltnp "sport = :$p" 2>/dev/null | sed 's/^/  /' || true
    elif command -v fuser >/dev/null 2>&1; then
      fuser -n tcp "$p" 2>/dev/null | sed 's/^/  /' || true
    fi
  done
  echo
  echo "── State-файл (${RUN_DIR}/children.json) ──"
  if [ -f "${RUN_DIR}/children.json" ]; then
    cat "${RUN_DIR}/children.json" | sed 's/^/  /'
  else
    echo "  (нет — daemon не запущен или прошлый запуск завершился штатно)"
  fi
}

cmd_cleanup() {
  prepare_runtime_env >/dev/null 2>&1 || true
  cleanup_stale
  ok "cleanup завершён."
}

# ── Dispatch ────────────────────────────────────────────────────────────────
case "$CMD" in
  start)   cmd_start   ;;
  stop)    cmd_stop    ;;
  status)  cmd_status  ;;
  restart) cmd_restart ;;
  logs)    cmd_logs    ;;
  doctor)  cmd_doctor  ;;
  cleanup) cmd_cleanup ;;
  run|*)   cmd_run     ;;
esac
