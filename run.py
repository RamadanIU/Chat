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

import argparse
import atexit
import base64
import http.server
import json
import os
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── Куда складываем state-файл прошлого запуска (PID/PGID детей). ────────────
# Используем тот же RUN_DIR, что и start.sh (~/.cache/chat-stack), чтобы внешний
# watchdog/start.sh мог дочистить сирот после аварии run.py.
RUN_DIR = Path(os.environ.get("AGENT_PRO_RUN_DIR", str(Path.home() / ".cache" / "chat-stack")))
STATE_FILE = RUN_DIR / "children.json"

# ── Порты / хосты ────────────────────────────────────────────────────────────
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "8080"))
WORKSPACE_PORT = int(os.environ.get("WORKSPACE_PORT", "8764"))
TERM_PORT = int(os.environ.get("TERM_PORT", "8765"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "7777"))

HOST = os.environ.get("HOST", "0.0.0.0")
# Бридж по умолчанию слушает на том же интерфейсе, что и terminal/workspace,
# чтобы быть доступным из браузера, открытого на другом устройстве (телефон,
# другой комп в LAN, туннель). Если нужно жёстко ограничить только локалхостом —
# задайте AGENT_PRO_BRIDGE_HOST=127.0.0.1 (или HOST=127.0.0.1 для всех сервисов).
BRIDGE_HOST = os.environ.get("AGENT_PRO_BRIDGE_HOST", HOST)

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
    # Глотаем любые ошибки записи в stdout: при закрытии окна терминала наш
    # tty уезжает, write() может бросить BrokenPipeError/OSError. Это не повод
    # ронять supervisor — сервисы в своих setsid-группах живут дальше.
    try:
        print(f"{color}[{name:<9}]{reset} {line}", flush=True)
    except Exception:
        pass


# ── Помощники: state-файл и убийство сирот ───────────────────────────────────
def _state_load() -> dict:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _state_save(state: dict) -> None:
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh)
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log("system", f"WARN: не смог записать state-файл {STATE_FILE}: {exc}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _kill_pgid(pgid: int, sig: int = signal.SIGTERM) -> None:
    if pgid <= 0:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _find_pids_listening(port: int) -> list[int]:
    """Best-effort: вернуть PID-ы процессов, слушающих локальный TCP-порт.

    Пробуем по очереди: lsof, ss, fuser. Если ни одного нет — возвращаем [].
    """
    pids: set[int] = set()
    for cmd in (
        ["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
        ["fuser", "-n", "tcp", str(port)],
        ["ss", "-ltnpH", "sport", "= :%d" % port],
    ):
        bin_ = shutil.which(cmd[0])
        if not bin_:
            continue
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for token in out.replace(",", " ").split():
            # ss выдаёт "users:((\"node\",pid=1234,fd=21))"
            if "pid=" in token:
                try:
                    pids.add(int(token.split("pid=", 1)[1].split(",", 1)[0].rstrip(")")))
                except ValueError:
                    pass
                continue
            # lsof / fuser — голые числа
            try:
                pid = int(token.strip("p"))
                if pid > 0:
                    pids.add(pid)
            except ValueError:
                pass
        if pids:
            break
    return sorted(pids)


def _port_is_free(host: str, port: int) -> bool:
    """Проверить, свободен ли TCP-порт на host."""
    bind_host = host if host not in ("", "0.0.0.0", "::") else "0.0.0.0"
    fam = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((bind_host, port))
            return True
        except OSError:
            return False
    finally:
        s.close()


def _free_port(port: int, label: str, our_pid: int | None = None) -> None:
    """Best-effort: убить любого, кто держит TCP-порт. Не трогаем self/our_pid."""
    pids = [p for p in _find_pids_listening(port) if p != os.getpid() and p != our_pid]
    if not pids:
        return
    log("system", f"port {port} ({label}) занят PID {pids} — пытаюсь освободить (TERM)")
    for pid in pids:
        _kill_pid(pid, signal.SIGTERM)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        alive = [p for p in pids if _pid_alive(p)]
        if not alive:
            return
        time.sleep(0.2)
    alive = [p for p in pids if _pid_alive(p)]
    if alive:
        log("system", f"port {port} ({label}): TERM не помог, шлю KILL → {alive}")
        for pid in alive:
            _kill_pid(pid, signal.SIGKILL)
        time.sleep(0.3)


def cleanup_stale_children() -> None:
    """Убить процессы из state-файла прошлого запуска (если живы) и почистить порты.

    Делается на старте run.py, чтобы перезапуск после аварии не утыкался в
    «address already in use» и не оставлял сирот.
    """
    state = _state_load()
    children = state.get("children") or []
    if children:
        log("system", f"cleanup: state-файл содержит {len(children)} прошлых детей — снимаю")
    # Сначала TERM по PGID (накроет всю группу), затем KILL.
    for entry in children:
        pgid = int(entry.get("pgid") or 0)
        pid = int(entry.get("pid") or 0)
        if pgid:
            _kill_pgid(pgid, signal.SIGTERM)
        elif pid:
            _kill_pid(pid, signal.SIGTERM)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        any_alive = any(_pid_alive(int(e.get("pid") or 0)) for e in children)
        if not any_alive:
            break
        time.sleep(0.2)
    for entry in children:
        pgid = int(entry.get("pgid") or 0)
        pid = int(entry.get("pid") or 0)
        if pid and _pid_alive(pid):
            if pgid:
                _kill_pgid(pgid, signal.SIGKILL)
            else:
                _kill_pid(pid, signal.SIGKILL)

    # Затем — порты: даже если прошлый PID уже мёртв, мог остаться независимый
    # процесс (например, ручной node server.js).
    for label, port in (
        ("frontend", FRONTEND_PORT),
        ("workspace", WORKSPACE_PORT),
        ("terminal", TERM_PORT),
        ("bridge", BRIDGE_PORT),
    ):
        _free_port(port, label, our_pid=os.getpid())

    # Очистим state-файл (новые дети запишутся в него по мере старта).
    _state_save({"children": []})


def _state_register_child(name: str, pid: int, pgid: int) -> None:
    state = _state_load()
    children = state.get("children") or []
    children = [c for c in children if int(c.get("pid") or 0) != pid]
    children.append({"name": name, "pid": pid, "pgid": pgid, "started_at": time.time()})
    state["children"] = children
    state["parent_pid"] = os.getpid()
    state["updated_at"] = time.time()
    _state_save(state)


def _state_unregister_child(pid: int) -> None:
    state = _state_load()
    children = [c for c in (state.get("children") or []) if int(c.get("pid") or 0) != pid]
    state["children"] = children
    state["updated_at"] = time.time()
    _state_save(state)


def _state_clear() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def banner() -> None:
    log("system", "─" * 64)
    log("system", "Agent Pro — единый запуск (run.py)")
    log("system", "─" * 64)
    log("system", f"Frontend       : http://localhost:{FRONTEND_PORT}")
    log("system", f"Workspace API  : http://localhost:{WORKSPACE_PORT}/ws/ping")
    log("system", f"Terminal (ws)  : ws://localhost:{TERM_PORT}/term  | /exec")
    bridge_display = BRIDGE_HOST if BRIDGE_HOST not in ("0.0.0.0", "::", "") else "localhost"
    log("system", f"MCP bridge (ws): ws://{bridge_display}:{BRIDGE_PORT}")
    ab = shutil.which("agent-browser")
    if ab:
        log("system", f"agent-browser  : {ab} (browser_action в чате готов)")
    else:
        log("system", "agent-browser  : НЕ установлен — browser_action будет падать.")
        log("system", "                 (поставьте через bash start.sh либо tools/agent-browser-termux/install.sh)")
    ba = shutil.which("browser-act")
    if ba:
        log("system", f"BrowserAct CLI : {ba} (browser_act в чате готов)")
    else:
        log("system", "BrowserAct CLI : НЕ установлен — browser_act будет падать.")
        log("system", "                 (поставьте через bash start.sh либо uv tool install browser-act-cli --python 3.12)")
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

    def __init__(
        self,
        name: str,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        ports: list[int] | None = None,
    ):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.env = {**os.environ, **(env or {})}
        self.ports = ports or []
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.pgid: int = 0
        # Бюджет рестартов: храним timestamps последних запусков, чтобы видеть,
        # как часто сервис падает и применять backoff.
        self.restart_history: deque[float] = deque(maxlen=20)
        self.next_allowed_start: float = 0.0

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _backoff_seconds(self) -> float:
        """Экспоненциальный backoff в зависимости от того, сколько раз сервис
        падал за последние 60 секунд. 0,1 → 1 → 2 → 5 → 10 → 30 → 60."""
        now = time.time()
        recent = [t for t in self.restart_history if now - t < 60.0]
        n = len(recent)
        if n <= 1:
            return 0.5
        if n == 2:
            return 1.0
        if n == 3:
            return 2.0
        if n == 4:
            return 5.0
        if n == 5:
            return 10.0
        if n == 6:
            return 30.0
        return 60.0

    def start(self) -> None:
        # Перед стартом — освободить наш порт, если кто-то его уже держит.
        for port in self.ports:
            _free_port(port, self.name, our_pid=os.getpid())

        self.restart_history.append(time.time())
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
        try:
            self.pgid = os.getpgid(self.proc.pid) if os.name == "posix" else self.proc.pid
        except (ProcessLookupError, OSError):
            self.pgid = self.proc.pid
        _state_register_child(self.name, self.proc.pid, self.pgid)
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            log(self.name, raw.rstrip("\n"))
        rc = self.proc.wait()
        if self.proc:
            _state_unregister_child(self.proc.pid)
        log("system", f"{self.name} завершился с кодом {rc}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            if self.proc:
                _state_unregister_child(self.proc.pid)
            return
        log("system", f"stop {self.name} (pid={self.proc.pid})")
        pid = self.proc.pid
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            else:
                self.proc.terminate()
        except ProcessLookupError:
            _state_unregister_child(pid)
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
        _state_unregister_child(pid)


# ── Встроенный статический сервер для index.html ─────────────────────────────
RUNTIME_CONFIG_MARKER = "<!-- AGENT_PRO_RUNTIME_CONFIG_INJECT -->"


def _runtime_config_payload(host_for_browser: str) -> dict:
    """Значения, которые index.html подхватит как window.AGENT_PRO_DEFAULTS.

    Используются как нативные дефолты для полей «Терминал» и «Файловая система»
    в настройках, чтобы из коробки работало подключение на ws://<host>:<TERM_PORT>
    и http://<host>:<WORKSPACE_PORT> без ручного ввода.
    """
    # Если бридж жёстко привязан к loopback (AGENT_PRO_BRIDGE_HOST=127.0.0.1) —
    # отдаём его как есть; иначе строим URL относительно того хоста, по которому
    # пришёл запрос на frontend (так же, как для terminal/workspace).
    if BRIDGE_HOST in ("", "0.0.0.0", "::"):
        bridge_url = f"ws://{host_for_browser}:{BRIDGE_PORT}"
    else:
        bridge_url = f"ws://{BRIDGE_HOST}:{BRIDGE_PORT}"
    return {
        "termUrl": f"ws://{host_for_browser}:{TERM_PORT}",
        "wsApiUrl": f"http://{host_for_browser}:{WORKSPACE_PORT}",
        "bridgeUrl": bridge_url,
        "termPort": TERM_PORT,
        "wsApiPort": WORKSPACE_PORT,
        "bridgePort": BRIDGE_PORT,
    }


def _build_runtime_config_script(host_for_browser: str) -> str:
    import json as _json
    payload = _runtime_config_payload(host_for_browser)
    return (
        "<script>window.AGENT_PRO_DEFAULTS = "
        + _json.dumps(payload, ensure_ascii=False)
        + ";</script>"
    )


class FrontendHandler(http.server.SimpleHTTPRequestHandler):
    """Отдаёт index.html и прочие статические файлы из корня репо.

    Если задан _AUTH_TOKEN — каждый запрос требует HTTP Basic Auth.
    Дополнительно в index.html подставляются нативные адреса локальных сервисов
    (терминал, Workspace API), чтобы UI знал, к чему подключаться из коробки.
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

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _is_index_request(self) -> bool:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        return path in ("/", "/index.html")

    def _host_for_browser(self) -> str:
        """Хост, который браузер должен использовать для ws://termUrl и http://wsApiUrl.

        Берём из заголовка Host (то, что написано в адресной строке),
        отрезая порт. Если HOST=0.0.0.0/:: и заголовка нет — вернём 'localhost'.
        """
        raw = self.headers.get("Host", "") or ""
        # IPv6 в Host: [::1]:8080
        if raw.startswith("["):
            end = raw.find("]")
            if end != -1:
                return raw[1:end] or "localhost"
        host = raw.split(":", 1)[0].strip()
        if host and host not in ("0.0.0.0", "::"):
            return host
        return "localhost"

    def _serve_index(self) -> None:
        index_path = ROOT / "index.html"
        try:
            raw = index_path.read_bytes()
        except OSError as exc:
            self.send_error(500, f"index.html unavailable: {exc}")
            return

        host_for_browser = self._host_for_browser()
        injection = _build_runtime_config_script(host_for_browser)
        marker_b = RUNTIME_CONFIG_MARKER.encode("utf-8")
        if marker_b in raw:
            patched = raw.replace(marker_b, injection.encode("utf-8"), 1)
        else:
            # Маркера нет (например, старая копия файла) — просто отдаём как есть.
            patched = raw

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(patched)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(patched)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._challenge()
            return
        if self._is_index_request():
            self._serve_index()
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
    """Встроенный статический сервер. Если упал — supervisor поднимет заново."""

    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.httpd: socketserver.TCPServer | None = None
        self._stopped = False
        self.bind_ok = threading.Event()

    def run(self) -> None:
        socketserver.TCPServer.allow_reuse_address = True
        # Несколько попыток bind: после краша порт может ещё пару секунд
        # висеть в TIME_WAIT/занят сиротой.
        last_exc: Exception | None = None
        for _ in range(20):
            if self._stopped:
                return
            _free_port(self.port, "frontend", our_pid=os.getpid())
            try:
                self.httpd = socketserver.ThreadingTCPServer(
                    (self.host, self.port), FrontendHandler
                )
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.5)
        if self.httpd is None:
            log("system", f"frontend: не удалось занять порт {self.port}: {last_exc}")
            return
        self.bind_ok.set()
        log("frontend", f"serving {ROOT} on http://{self.host}:{self.port}")
        try:
            self.httpd.serve_forever()
        except Exception as exc:
            log("system", f"frontend завершился: {exc}")
        finally:
            try:
                if self.httpd is not None:
                    self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None

    def stop(self) -> None:
        self._stopped = True
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            try:
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None


# ── Поиск python для wsapi (предпочтительно из .venv) ────────────────────────
def find_python() -> str:
    venv_py = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if venv_py.is_file():
        return str(venv_py)
    return sys.executable or shutil.which("python3") or "python3"


# ── main ─────────────────────────────────────────────────────────────────────
def _build_services() -> list[Service]:
    services: list[Service] = []
    services.append(Service(
        name="workspace",
        cmd=[find_python(), "wsapi_server.py", "--host", HOST, "--port", str(WORKSPACE_PORT)],
        cwd=ROOT,
        ports=[WORKSPACE_PORT],
    ))
    services.append(Service(
        name="terminal",
        cmd=["node", "server.js"],
        cwd=ROOT,
        env={"PORT": str(TERM_PORT)},
        ports=[TERM_PORT],
    ))
    services.append(Service(
        name="bridge",
        cmd=["node", "agent-pro-bridge.mjs"],
        cwd=ROOT / "bridge",
        env={"AGENT_PRO_BRIDGE_HOST": BRIDGE_HOST, "AGENT_PRO_BRIDGE_PORT": str(BRIDGE_PORT)},
        ports=[BRIDGE_PORT],
    ))
    return services


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agent Pro — оркестратор сервисов с авто-рестартом."
    )
    p.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Только освободить порты/прибить сирот предыдущего запуска и выйти.",
    )
    p.add_argument(
        "--no-restart",
        action="store_true",
        help="Не перезапускать упавшие сервисы (старое поведение: один за всех).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.cleanup_only:
        log("system", "cleanup-only: убираю остатки прошлого запуска")
        cleanup_stale_children()
        log("system", "cleanup-only: готово")
        return 0

    banner()

    if not (ROOT / "index.html").is_file():
        log("system", "ERROR: index.html не найден в корне репозитория.")
        return 1
    if not (ROOT / "node_modules").is_dir():
        log("system", "node_modules не найден — запустите ./start.sh для установки зависимостей.")
        return 1
    if not (ROOT / "bridge" / "node_modules").is_dir():
        log("system", "bridge/node_modules не найден — запустите ./start.sh.")
        return 1

    # Перед стартом — снять любых сирот / освободить порты.
    cleanup_stale_children()

    services = _build_services()
    frontend = FrontendServer(HOST, FRONTEND_PORT)

    stop_event = threading.Event()
    shutdown_lock = threading.Lock()
    shutdown_done = threading.Event()

    def do_shutdown() -> None:
        with shutdown_lock:
            if shutdown_done.is_set():
                return
            shutdown_done.set()
        for svc in services:
            try:
                svc.stop()
            except Exception as exc:  # pragma: no cover
                log("system", f"stop {svc.name} ошибка: {exc}")
        try:
            frontend.stop()
        except Exception as exc:  # pragma: no cover
            log("system", f"stop frontend ошибка: {exc}")
        # Финальная подчистка портов на случай SIGKILL у детей.
        for label, port in (
            ("frontend", FRONTEND_PORT),
            ("workspace", WORKSPACE_PORT),
            ("terminal", TERM_PORT),
            ("bridge", BRIDGE_PORT),
        ):
            _free_port(port, label, our_pid=os.getpid())
        _state_clear()
        log("system", "все сервисы остановлены.")

    atexit.register(do_shutdown)

    def on_signal(signum, _frame):
        log("system", f"получен сигнал {signum}, останавливаю сервисы…")
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    # SIGHUP сознательно ИГНОРИРУЕМ. Закрытие окна терминала (в foreground-режиме
    # `bash start.sh` / `npm start`) приходит сюда как SIGHUP — раньше мы по нему
    # гасили весь стек, включая bridge. Теперь run.py остаётся жить, а дочерние
    # сервисы и так лежат в отдельных process group'ах через os.setsid и
    # SIGHUP от tty не получают. Остановка — только по явному SIGINT/SIGTERM
    # (Ctrl+C, `bash start.sh stop`, kill).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Старт
    try:
        frontend.start()
        for svc in services:
            try:
                svc.start()
            except Exception as exc:
                log("system", f"не удалось стартовать {svc.name}: {exc}")
            time.sleep(0.2)
    except Exception as exc:
        log("system", f"FATAL при старте: {exc}")
        do_shutdown()
        return 1

    # ── Supervisor loop ──────────────────────────────────────────────────────
    # Раз в секунду проверяем каждый сервис. Если упал — рестарт с backoff.
    # При --no-restart работаем по-старому: один упал → выходим.
    try:
        while not stop_event.is_set():
            now = time.time()

            # Frontend
            if not frontend.is_alive() and not stop_event.is_set():
                if args.no_restart:
                    log("system", "frontend упал — завершаю всё (--no-restart).")
                    stop_event.set()
                    break
                log("system", "frontend упал — перезапускаю")
                frontend = FrontendServer(HOST, FRONTEND_PORT)
                try:
                    frontend.start()
                except Exception as exc:
                    log("system", f"не смог перезапустить frontend: {exc}")

            # Сервисы-дети
            for svc in services:
                if svc.is_alive():
                    continue
                if args.no_restart:
                    log("system", f"{svc.name} упал — завершаю всё (--no-restart).")
                    stop_event.set()
                    break
                if svc.next_allowed_start == 0.0:
                    # Только что упал — планируем рестарт с backoff.
                    rc = svc.proc.returncode if svc.proc else None
                    wait = svc._backoff_seconds()
                    log(
                        "system",
                        f"{svc.name} упал (rc={rc}). Перезапуск через {wait:.1f}s "
                        f"(падений за 60s: "
                        f"{sum(1 for t in svc.restart_history if now - t < 60.0)})",
                    )
                    svc.next_allowed_start = now + wait
                    continue
                if now >= svc.next_allowed_start:
                    try:
                        svc.start()
                        svc.next_allowed_start = 0.0
                    except Exception as exc:
                        log("system", f"start {svc.name} ошибка: {exc}")
                        svc.next_allowed_start = now + 5.0

            stop_event.wait(timeout=1.0)
    except Exception as exc:
        log("system", f"supervisor исключение: {exc}")
    finally:
        do_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
