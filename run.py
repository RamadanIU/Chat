#!/usr/bin/env python3
"""
run.py — единый оркестратор всех сервисов проекта.

Запускает в одном процессе четыре дочерних сервиса и отдаёт frontend
(`index.html`) на локальном HTTP-порту:

    Frontend (index.html)        http://localhost:8080
    Workspace API (Flask)        http://localhost:8764/ws/...
    Terminal Server (Node, ws)   ws://localhost:8765/term  |  /exec
    MCP stdio Bridge (Node, ws)  ws://localhost:7777

Каждый сервис запускается как отдельный subprocess; их stdout/stderr
префиксуются цветом и именем и пишутся в этот же терминал. По Ctrl+C
все дочерние процессы останавливаются корректно (через process group).

Конфигурация портов — через переменные окружения:
    FRONTEND_PORT (default 8080)
    WORKSPACE_PORT (default 8764)
    TERM_PORT (default 8765)
    BRIDGE_PORT (default 7777)
    HOST (default 0.0.0.0 для backend, 127.0.0.1 для bridge)

Запуск:
    python3 run.py
    # или
    bash start.sh           # ставит зависимости и зовёт run.py
    # или
    npm start               # то же самое через package.json
"""
from __future__ import annotations

import base64
import http.server
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── Порты / хосты ────────────────────────────────────────────────────────────
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "8080"))
WORKSPACE_PORT = int(os.environ.get("WORKSPACE_PORT", "8764"))
TERM_PORT = int(os.environ.get("TERM_PORT", "8765"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "7777"))

HOST = os.environ.get("HOST", "0.0.0.0")
BRIDGE_HOST = os.environ.get("AGENT_PRO_BRIDGE_HOST", "127.0.0.1")

# ── HTTP Basic Auth для frontend ─────────────────────────────────────────────
# Дефолтные креды по просьбе владельца проекта; меняются через env. Чтобы
# совсем убрать авторизацию — установите AUTH_DISABLE=1.
AUTH_USER = os.environ.get("AUTH_USER", "Ramadan")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "Bismillah2021")
AUTH_DISABLE = os.environ.get("AUTH_DISABLE", "").lower() in ("1", "true", "yes", "on")
AUTH_REALM = os.environ.get("AUTH_REALM", "Agent Pro")
_AUTH_TOKEN = (
    None
    if AUTH_DISABLE or not AUTH_USER
    else base64.b64encode(f"{AUTH_USER}:{AUTH_PASSWORD}".encode("utf-8")).decode("ascii")
)

# ── ANSI-цвета для префиксов ─────────────────────────────────────────────────
COLORS = {
    "frontend":  "\033[36m",  # cyan
    "workspace": "\033[32m",  # green
    "terminal":  "\033[33m",  # yellow
    "bridge":    "\033[35m",  # magenta
    "system":    "\033[1;34m",  # bold blue
}
RESET = "\033[0m"
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def log(name: str, line: str) -> None:
    color = COLORS.get(name, "") if USE_COLOR else ""
    reset = RESET if USE_COLOR else ""
    print(f"{color}[{name:<9}]{reset} {line}", flush=True)


def banner() -> None:
    log("system", "─" * 64)
    log("system", "Agent Pro — единый запуск (run.py)")
    log("system", "─" * 64)
    log("system", f"Frontend       : http://localhost:{FRONTEND_PORT}")
    log("system", f"Workspace API  : http://localhost:{WORKSPACE_PORT}/ws/ping")
    log("system", f"Terminal (ws)  : ws://localhost:{TERM_PORT}/term  | /exec")
    log("system", f"MCP bridge (ws): ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
    ab = shutil.which("agent-browser")
    if ab:
        log("system", f"agent-browser  : {ab} (browser_action в чате готов)")
    else:
        log("system", "agent-browser  : НЕ установлен — browser_action будет падать.")
        log("system", "                 (поставьте через bash start.sh либо tools/agent-browser-termux/install.sh)")
    log("system", "─" * 64)
    if _AUTH_TOKEN is None:
        log("system", "Auth (frontend): ОТКЛЮЧЕНА (AUTH_DISABLE=1)")
    else:
        log("system", f"Auth (frontend): login={AUTH_USER}  password={AUTH_PASSWORD}")
        log("system", "                 (меняется через AUTH_USER / AUTH_PASSWORD env)")
    log("system", "Открой Frontend в браузере и в Settings укажи адреса выше.")
    log("system", "Ctrl+C — остановить все сервисы.")
    log("system", "─" * 64)


# ── Сервисы ──────────────────────────────────────────────────────────────────
class Service:
    """Один дочерний процесс с префиксованным выводом."""

    def __init__(self, name: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        log("system", f"start {self.name}: {' '.join(self.cmd)} (cwd={self.cwd})")
        kwargs: dict = dict(
            cwd=str(self.cwd),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )
        # На POSIX запускаем в новой process group, чтобы убить всё дерево
        if os.name == "posix":
            kwargs["preexec_fn"] = os.setsid
        self.proc = subprocess.Popen(self.cmd, **kwargs)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            log(self.name, raw.rstrip("\n"))
        rc = self.proc.wait()
        log("system", f"{self.name} завершился с кодом {rc}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        log("system", f"stop {self.name} (pid={self.proc.pid})")
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            else:
                self.proc.terminate()
        except ProcessLookupError:
            return
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                else:
                    self.proc.kill()
            except ProcessLookupError:
                pass


# ── Встроенный статический сервер для index.html ─────────────────────────────
class FrontendHandler(http.server.SimpleHTTPRequestHandler):
    """Отдаёт index.html и прочие статические файлы из корня репо.

    Если задан _AUTH_TOKEN — каждый запрос требует HTTP Basic Auth.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log("frontend", fmt % args)

    # ── Basic Auth ───────────────────────────────────────────────────────────
    def _authorized(self) -> bool:
        if _AUTH_TOKEN is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        return header.split(" ", 1)[1].strip() == _AUTH_TOKEN

    def _challenge(self) -> None:
        body = b"Authorization required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._challenge()
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._authorized():
            self._challenge()
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._challenge()
            return
        # SimpleHTTPRequestHandler не реализует POST; ответим 405.
        self.send_error(405, "POST not supported")


class FrontendServer(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.httpd: socketserver.TCPServer | None = None

    def run(self) -> None:
        socketserver.TCPServer.allow_reuse_address = True
        try:
            self.httpd = socketserver.ThreadingTCPServer((self.host, self.port), FrontendHandler)
        except OSError as exc:
            log("system", f"frontend: не удалось занять порт {self.port}: {exc}")
            return
        log("frontend", f"serving {ROOT} on http://{self.host}:{self.port}")
        try:
            self.httpd.serve_forever()
        except Exception as exc:
            log("system", f"frontend завершился: {exc}")

    def stop(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass


# ── Поиск python для wsapi (предпочтительно из .venv) ────────────────────────
def find_python() -> str:
    venv_py = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable or shutil.which("python3") or "python3"


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    banner()

    if not (ROOT / "index.html").is_file():
        log("system", "ERROR: index.html не найден в корне репозитория.")
        return 1

    services: list[Service] = []

    # 1. Workspace API (Python Flask)
    services.append(Service(
        name="workspace",
        cmd=[find_python(), "wsapi_server.py", "--host", HOST, "--port", str(WORKSPACE_PORT)],
        cwd=ROOT,
    ))

    # 2. Terminal Server (Node)
    if not (ROOT / "node_modules").is_dir():
        log("system", "node_modules не найден — запустите ./start.sh для установки зависимостей.")
        return 1
    services.append(Service(
        name="terminal",
        cmd=["node", "server.js"],
        cwd=ROOT,
        env={"PORT": str(TERM_PORT)},
    ))

    # 3. MCP stdio Bridge (Node)
    if not (ROOT / "bridge" / "node_modules").is_dir():
        log("system", "bridge/node_modules не найден — запустите ./start.sh.")
        return 1
    services.append(Service(
        name="bridge",
        cmd=["node", "agent-pro-bridge.mjs"],
        cwd=ROOT / "bridge",
        env={"AGENT_PRO_BRIDGE_HOST": BRIDGE_HOST, "AGENT_PRO_BRIDGE_PORT": str(BRIDGE_PORT)},
    ))

    # 4. Frontend (встроенный)
    frontend = FrontendServer(HOST, FRONTEND_PORT)
    frontend.start()

    for svc in services:
        svc.start()
        time.sleep(0.2)  # небольшой разрыв, чтобы лог не мешался

    stop_event = threading.Event()

    def shutdown(signum, _frame):
        log("system", f"получен сигнал {signum}, останавливаю сервисы…")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stop_event.is_set():
            # Если какой-то критический сервис умер — выходим (чтобы пользователь видел)
            for svc in services:
                if svc.proc and svc.proc.poll() is not None:
                    log("system", f"{svc.name} упал — останавливаю остальное.")
                    stop_event.set()
                    break
            time.sleep(1)
    finally:
        for svc in services:
            svc.stop()
        frontend.stop()

    log("system", "все сервисы остановлены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
