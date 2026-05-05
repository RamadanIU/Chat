"""Workspace API — простой HTTP-сервер для файловой системы агента + памяти.

Endpoints (workspace):
    GET  /ws/ping                          — healthcheck
    GET  /ws/config                        — текущая рабочая область + список разрешённых корней
    POST /ws/config   {workspace_dir}      — сменить рабочую область на лету
    GET  /ws/list?path=&depth=             — рекурсивный обход
    GET  /ws/info?path=                    — метаданные одного пути (file/dir/size/lines/binary)
    GET  /ws/read?path=                    — содержимое файла (base64)
    POST /ws/write    {path, content, ...} — запись (overwrite)
    POST /ws/append   {path, content, ...} — дописать в конец (без read+write на клиенте)
    POST /ws/rm       {path, recursive}    — удалить
    POST /ws/mkdir    {path}               — создать папку
    POST /ws/move     {src, dest}          — переместить/переименовать
    POST /ws/copy     {src, dest, overwrite} — копировать (file/dir, рекурсивно)
    GET  /ws/exists?path=                  — проверка существования
    GET  /ws/search?query=&...             — поиск по содержимому файлов
    POST /ws/reset                         — очистить рабочую область

Endpoints (NVIDIA NIM reverse-proxy — для обхода CORS из браузера):
    ANY  /nvidia/<path>                    — прозрачный реверс-прокси к
                                              integrate.api.nvidia.com (или к
                                              X-Nvidia-Base-Url из заголовка),
                                              включая SSE-стриминг и tool calls.
                                              Заменяет внешние CORS-прокси
                                              вроде corsproxy.io.

Endpoints (Ollama reverse-proxy — для обхода CORS из браузера):
    ANY  /ollama/<path>                    — прозрачный реверс-прокси к
                                              http://localhost:11434 (или к
                                              X-Ollama-Base-Url из заголовка).
                                              Поддерживает SSE-стрим, ndjson
                                              (для /api/pull) и Ollama Cloud
                                              (https://ollama.com).

Endpoints (memory — БД-бэкенд для всего, что раньше жило в localStorage):
    GET  /mem/health                       — статус БД, счётчики
    GET  /mem/export                       — полный снэпшот (kv + чаты + скилы + MCP)
    POST /mem/import   {…}                 — атомарно заменить содержимое БД
                                              (формат: localStorage shape ИЛИ /mem/export shape)
    POST /mem/sync     {set, delete}       — инкрементальные апдейты по ключам
    POST /mem/reset                        — полная очистка БД памяти
    GET  /mem/kv?key=                      — конкретный ключ или весь дамп kv
    POST /mem/kv       {key, value}        — выставить ключ (value=null → удалить)
    DELETE /mem/kv?key=                    — удалить ключ
    GET  /mem/chats                        — список чатов с сообщениями
    GET  /mem/chats/<id>                   — конкретный чат
    POST /mem/chats    {id, name, …}       — upsert чата (вместе с messages)
    DELETE /mem/chats/<id>                 — удалить чат
    GET  /mem/skills                       — все скилы
    POST /mem/skills   {skills:[…]}        — полная замена набора скилов
    DELETE /mem/skills/<id>                — удалить скил
    GET  /mem/mcp/servers                  — все MCP-серверы
    POST /mem/mcp/servers {servers:[…]}    — полная замена набора серверов
    DELETE /mem/mcp/servers/<id>           — удалить сервер

Endpoints (system — обновление установки):
    GET  /sys/version                      — путь к репо, ветка, текущий коммит
    GET  /sys/update/check                 — git fetch и проверка свежих коммитов
    POST /sys/update/apply                 — git pull --ff-only и переустановка зависимостей

Конфигурация (env):
    WORKSPACE_DIR       начальная рабочая область (default: ~/storage/shared/workspace,
                        fallback ~/workspace, если первой не существует)
    WORKSPACE_ROOTS     :-разделённый список разрешённых корней для смены пути
                        (default: $HOME). Любой путь, в который клиент пытается
                        переключиться через POST /ws/config, должен лежать внутри
                        одного из этих корней.
    WORKSPACE_HOST      адрес для прослушивания (default: 0.0.0.0)
    WORKSPACE_PORT      порт (default: 8764)
    AGENT_PRO_MEMORY_DB путь к SQLite-файлу памяти (default: ~/.local/share/agent-pro/memory.db)
    NVIDIA_BASE_URL     дефолтный upstream для /nvidia/<path>
                        (default: https://integrate.api.nvidia.com/v1).
                        Клиент может переопределить заголовком X-Nvidia-Base-Url.
    NVIDIA_PROXY_TIMEOUT таймаут чтения upstream в секундах для /nvidia/<path>
                        (default: 900). SSE может тянуться долго —
                        ставьте побольше.
    OLLAMA_BASE_URL     дефолтный upstream для /ollama/<path>
                        (default: http://localhost:11434). Клиент может
                        переопределить заголовком X-Ollama-Base-Url.
    OLLAMA_PROXY_TIMEOUT таймаут чтения upstream для /ollama/<path>
                        (default: 900). Pull большой модели — это часы,
                        ставьте по нужде.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import socket
import subprocess
import sys
from typing import Iterable
from urllib import error as _urllib_error
from urllib import parse as _urllib_parse
from urllib import request as _urllib_request

from flask import Flask, Response, abort, jsonify, request, stream_with_context
from flask_cors import CORS

from memory_db import MemoryDB

app = Flask(__name__)
CORS(app)

# Глобальная БД памяти. Создаём лениво, чтобы тесты могли подменять путь.
_memory_db: MemoryDB | None = None


def memory_db() -> MemoryDB:
    global _memory_db
    if _memory_db is None:
        _memory_db = MemoryDB()
    return _memory_db


# ═══════════════════════ КОНФИГУРАЦИЯ ═══════════════════════

def _default_workspace() -> str:
    """Подобрать дефолтный путь рабочей области.

    Сначала пробуем Termux-путь (~/storage/shared/workspace), для обратной
    совместимости с существующими установками. Если такого пути нет — берём
    ~/workspace.
    """
    env = os.environ.get("WORKSPACE_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    termux = os.path.expanduser("~/storage/shared/workspace")
    if os.path.isdir(termux):
        return termux
    return os.path.expanduser("~/workspace")


def _default_roots() -> list[str]:
    """Список корней, в которые разрешено переключать рабочую область.

    Клиент не может выйти за их пределы при смене пути — это защита от
    `POST /ws/config {workspace_dir: "/"}` или подобных трюков.
    """
    env = os.environ.get("WORKSPACE_ROOTS", "").strip()
    if env:
        roots = [os.path.abspath(os.path.expanduser(p)) for p in env.split(":") if p.strip()]
        return [r for r in roots if r]
    return [os.path.expanduser("~")]


BASE_DIR: str = _default_workspace()
ALLOWED_ROOTS: list[str] = _default_roots()
os.makedirs(BASE_DIR, exist_ok=True)


def _is_under(path: str, parent: str) -> bool:
    """True, если `path` лежит внутри `parent` (или равен ему)."""
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(parent)]) == os.path.abspath(parent)
    except ValueError:
        # Разные диски (Windows) → точно не совпадают
        return False


def safe_path(rel_path: str) -> str:
    """Преобразовать относительный путь к абсолютному и убедиться, что он внутри BASE_DIR."""
    abs_path = os.path.abspath(os.path.join(BASE_DIR, rel_path or "."))
    if not _is_under(abs_path, BASE_DIR):
        abort(403, description="Доступ запрещён: путь вне рабочей области")
    return abs_path


# ═══════════════════════ ОБХОД И ПОИСК ═══════════════════════

# Папки, которые принципиально не имеет смысла обходить рекурсивно
# (огромные, не интересны агенту, регулярно убивают производительность).
_SEARCH_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build"}


def _walk_dir(root_abs: str, max_depth: int, current_depth: int = 0) -> list[dict]:
    """Собирает все файлы и папки внутри root_abs рекурсивно до max_depth."""
    result: list[dict] = []
    if current_depth > max_depth:
        return result
    try:
        entries = os.listdir(root_abs)
    except (FileNotFoundError, PermissionError):
        return result

    for name in entries:
        full = os.path.join(root_abs, name)
        rel = os.path.relpath(full, BASE_DIR).replace("\\", "/")
        try:
            stat = os.stat(full)
        except OSError:
            continue

        if os.path.isdir(full):
            result.append({"path": rel, "type": "dir", "bytes": 0, "mtime": stat.st_mtime})
            if current_depth < max_depth:
                result.extend(_walk_dir(full, max_depth, current_depth + 1))
        else:
            result.append({"path": rel, "type": "file", "bytes": stat.st_size, "mtime": stat.st_mtime})
    return result


def _iter_search_files(root_abs: str) -> Iterable[str]:
    """Обходит файлы под root_abs, пропуская тяжёлые системные папки."""
    for dirpath, dirnames, filenames in os.walk(root_abs):
        # in-place, чтобы os.walk не заходил внутрь
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP_DIRS]
        for fname in filenames:
            yield os.path.join(dirpath, fname)


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    # доля непечатных символов
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
    if not sample:
        return False
    nontext = sum(1 for b in sample if b not in text_chars)
    return (nontext / len(sample)) > 0.30


# ═══════════════════════ /ws/ping и /ws/config ═══════════════════════

@app.route("/ws/ping")
def ping():
    return jsonify({"ok": True, "workspace_dir": BASE_DIR})


@app.route("/ws/config", methods=["GET"])
def get_config():
    """Возвращает текущую рабочую область, свободное место и список разрешённых корней."""
    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except Exception:
        disk = None
    return jsonify({
        "workspace_dir": BASE_DIR,
        "allowed_roots": ALLOWED_ROOTS,
        "disk": disk,
        "exists": os.path.isdir(BASE_DIR),
    })


@app.route("/ws/config", methods=["POST"])
def set_config():
    """Сменить рабочую область на лету.

    Тело: {"workspace_dir": "/абсолютный/или/~/относительный/путь"}.
    Путь должен:
      • быть абсолютным после раскрытия `~`,
      • лежать внутри одного из ALLOWED_ROOTS,
      • либо уже существовать, либо быть создаваемым (создадим mkdir -p).
    """
    global BASE_DIR
    data = request.get_json(silent=True) or {}
    new_dir = (data.get("workspace_dir") or "").strip()
    if not new_dir:
        abort(400, description="workspace_dir не указан")

    abs_new = os.path.abspath(os.path.expanduser(new_dir))

    if not any(_is_under(abs_new, root) for root in ALLOWED_ROOTS):
        abort(403, description=(
            f"Путь {abs_new} вне разрешённых корней: {ALLOWED_ROOTS}. "
            "Перезапустите сервер с WORKSPACE_ROOTS=/your/root, чтобы расширить."
        ))

    try:
        os.makedirs(abs_new, exist_ok=True)
    except OSError as e:
        abort(400, description=f"Не удалось создать папку: {e}")

    BASE_DIR = abs_new
    return jsonify({"ok": True, "workspace_dir": BASE_DIR})


# ═══════════════════════ Чтение / запись / список ═══════════════════════

@app.route("/ws/list")
def list_files():
    path = request.args.get("path", ".")
    max_depth = int(request.args.get("depth", "10"))
    abs_path = safe_path(path)

    if not os.path.isdir(abs_path):
        abort(404, description="Не папка")

    files = _walk_dir(abs_path, max_depth)
    return jsonify({"files": files, "workspace_dir": BASE_DIR})


@app.route("/ws/info")
def info():
    """Метаданные одного пути: тип, размер, mtime, строки, бинарность."""
    path = request.args.get("path", "")
    if not path:
        abort(400, description="path не указан")
    abs_path = safe_path(path)
    if not os.path.exists(abs_path):
        abort(404, description="Не найден")

    try:
        stat = os.stat(abs_path)
    except OSError as e:
        abort(500, description=str(e))

    if os.path.isdir(abs_path):
        try:
            children = len(os.listdir(abs_path))
        except OSError:
            children = 0
        return jsonify({
            "path": path,
            "type": "dir",
            "bytes": 0,
            "mtime": stat.st_mtime,
            "children": children,
        })

    # file: попробуем определить бинарность и посчитать строки
    is_binary = False
    lines = 0
    try:
        with open(abs_path, "rb") as f:
            sample = f.read(8192)
        is_binary = _looks_binary(sample)
        if not is_binary:
            with open(abs_path, "rb") as f:
                # дёшево считаем переводы строк по сырым байтам
                lines = sum(buf.count(b"\n") for buf in iter(lambda: f.read(1 << 20), b""))
                if stat.st_size > 0:
                    # последняя строка без \n всё равно считается
                    lines = max(lines, 1) + (0 if sample.endswith(b"\n") else 1)
                    # коррекция выше неточна для пустого файла; компенсируем:
                    if lines == 0:
                        lines = 1
    except OSError:
        pass

    return jsonify({
        "path": path,
        "type": "file",
        "bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "lines": lines,
        "is_binary": is_binary,
    })


@app.route("/ws/read")
def read_file():
    path = request.args.get("path")
    if not path:
        abort(400, description="path не указан")
    abs_path = safe_path(path)
    if not os.path.isfile(abs_path):
        abort(404, description="Файл не найден")
    with open(abs_path, "rb") as f:
        content = f.read()
    return jsonify({"content": base64.b64encode(content).decode(), "encoding": "base64"})


def _decode_payload(data: dict) -> bytes:
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"])
    return data["content"].encode("utf-8")


@app.route("/ws/write", methods=["POST"])
def write_file():
    data = request.get_json(silent=True)
    if not data or "path" not in data or "content" not in data:
        abort(400, description="Нужны поля path и content")
    path = safe_path(data["path"])
    os.makedirs(os.path.dirname(path) or BASE_DIR, exist_ok=True)
    raw = _decode_payload(data)
    with open(path, "wb") as f:
        f.write(raw)
    return jsonify({"bytes": len(raw)})


@app.route("/ws/append", methods=["POST"])
def append_file():
    """Дописать данные в конец файла без round-trip read+write."""
    data = request.get_json(silent=True)
    if not data or "path" not in data or "content" not in data:
        abort(400, description="Нужны поля path и content")
    path = safe_path(data["path"])
    os.makedirs(os.path.dirname(path) or BASE_DIR, exist_ok=True)
    raw = _decode_payload(data)
    with open(path, "ab") as f:
        f.write(raw)
    return jsonify({"bytes": len(raw)})


@app.route("/ws/rm", methods=["POST"])
def rm():
    data = request.get_json(silent=True)
    if not data or "path" not in data:
        abort(400, description="path не указан")
    path = safe_path(data["path"])
    recursive = data.get("recursive", True)
    if not os.path.exists(path):
        abort(404, description="Не найден")
    if os.path.isdir(path):
        if recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    else:
        os.remove(path)
    return jsonify({"ok": True})


@app.route("/ws/mkdir", methods=["POST"])
def mkdir():
    data = request.get_json(silent=True) or {}
    if "path" not in data:
        abort(400, description="path не указан")
    path = safe_path(data["path"])
    os.makedirs(path, exist_ok=True)
    return jsonify({"ok": True})


@app.route("/ws/move", methods=["POST"])
def move():
    data = request.get_json(silent=True) or {}
    if "src" not in data or "dest" not in data:
        abort(400, description="Нужны поля src и dest")
    src = safe_path(data["src"])
    dest = safe_path(data["dest"])
    if not os.path.exists(src):
        abort(404, description="Источник не найден")
    os.makedirs(os.path.dirname(dest) or BASE_DIR, exist_ok=True)
    shutil.move(src, dest)
    return jsonify({"ok": True})


@app.route("/ws/copy", methods=["POST"])
def copy():
    """Копировать файл или директорию (рекурсивно) на стороне сервера."""
    data = request.get_json(silent=True) or {}
    if "src" not in data or "dest" not in data:
        abort(400, description="Нужны поля src и dest")
    src = safe_path(data["src"])
    dest = safe_path(data["dest"])
    overwrite = bool(data.get("overwrite", False))

    if not os.path.exists(src):
        abort(404, description="Источник не найден")
    if os.path.exists(dest) and not overwrite:
        abort(409, description="Назначение уже существует, передайте overwrite=true")

    os.makedirs(os.path.dirname(dest) or BASE_DIR, exist_ok=True)

    if os.path.isdir(src):
        if os.path.exists(dest) and overwrite:
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return jsonify({"ok": True, "type": "dir"})

    if os.path.exists(dest) and overwrite:
        os.remove(dest)
    shutil.copy2(src, dest)
    return jsonify({"ok": True, "type": "file", "bytes": os.path.getsize(dest)})


@app.route("/ws/exists")
def exists():
    try:
        return jsonify({"exists": os.path.exists(safe_path(request.args.get("path", ".")))})
    except Exception:
        return jsonify({"exists": False})


# ═══════════════════════ /ws/search ═══════════════════════

@app.route("/ws/search")
def search():
    """Поиск подстроки/regex по файлам рабочей области.

    Параметры:
        query           обязательный, искомая строка/регэксп
        path            каталог для поиска (default: ".")
        is_regex        bool (default: false)
        case_sensitive  bool (default: false)
        context_lines   int (default: 2)
        max_results     int (default: 500) — потолок, чтобы не повесить агента
    """
    query = request.args.get("query", "")
    if not query:
        abort(400, description="query не указан")

    rel = request.args.get("path", ".")
    is_regex = request.args.get("is_regex", "false").lower() == "true"
    case_sensitive = request.args.get("case_sensitive", "false").lower() == "true"
    context_lines = int(request.args.get("context_lines", "2"))
    max_results = int(request.args.get("max_results", "500"))

    root = safe_path(rel)
    if not os.path.isdir(root):
        abort(404, description="Не папка")

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if is_regex else re.escape(query), flags)

    results: list[dict] = []
    truncated = False

    for full in _iter_search_files(root):
        if len(results) >= max_results:
            truncated = True
            break
        try:
            with open(full, "rb") as f:
                head = f.read(8192)
            if _looks_binary(head):
                continue
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")
        except OSError:
            continue

        rel_path = os.path.relpath(full, BASE_DIR).replace("\\", "/")
        for idx, line_content in enumerate(lines):
            if pattern.search(line_content):
                ctx_before = [
                    {"line": i + 1, "content": lines[i]}
                    for i in range(max(0, idx - context_lines), idx)
                ]
                ctx_after = [
                    {"line": i + 1, "content": lines[i]}
                    for i in range(idx + 1, min(len(lines), idx + context_lines + 1))
                ]
                results.append({
                    "path": rel_path,
                    "line": idx + 1,
                    "content": line_content.strip(),
                    "context_before": ctx_before,
                    "context_after": ctx_after,
                })
                if len(results) >= max_results:
                    truncated = True
                    break

    return jsonify({"results": results, "total": len(results), "truncated": truncated})


# ═══════════════════════ /ws/reset ═══════════════════════

@app.route("/ws/reset", methods=["POST"])
def reset():
    """Очистить (удалить и пересоздать) текущую рабочую область."""
    if os.path.isdir(BASE_DIR):
        shutil.rmtree(BASE_DIR)
    os.makedirs(BASE_DIR, exist_ok=True)
    return jsonify({"ok": True})


# ═══════════════════════ /nvidia/* — реверс-прокси к NVIDIA NIM ═══════════
#
# Браузер не может ходить напрямую в integrate.api.nvidia.com из index.html —
# NVIDIA не отдаёт CORS-заголовки. Раньше для этого подключали внешние
# CORS-прокси (corsproxy.io и аналоги), что приводит к утечке nvapi-ключа
# на чужой сервер и зависимости от стороннего сервиса.
#
# Этот реверс-прокси решает обе проблемы: тот же origin, что и Workspace API
# (CORS уже разрешён через flask-cors), запрос форвардится в NVIDIA как есть
# (включая заголовок Authorization, тело и SSE-ответ).
#
# URL-схема:
#     /nvidia/chat/completions       → ${BASE}/chat/completions
#     /nvidia/models                 → ${BASE}/models
# где BASE = X-Nvidia-Base-Url (заголовок из браузера) или $NVIDIA_BASE_URL
# или https://integrate.api.nvidia.com/v1.

NVIDIA_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"

# Заголовки, которые НЕ форвардим в upstream:
#   - hop-by-hop (Connection, TE, Upgrade, …) согласно RFC 7230;
#   - служебные браузерные (Origin/Referer/Cookie) — NVIDIA их не ждёт;
#   - наш собственный X-Nvidia-Base-Url — он управляет прокси, а не upstream.
_NVIDIA_PROXY_SKIP_REQ_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "x-nvidia-base-url",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "origin",
    "referer",
    "cookie",
})

_NVIDIA_PROXY_SKIP_RESP_HEADERS = frozenset({
    "connection",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    # Werkzeug пересоберёт CORS-заголовки сам через flask_cors, чтобы не
    # дублировать их.
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-expose-headers",
})


def _nvidia_proxy_timeout() -> float:
    raw = os.environ.get("NVIDIA_PROXY_TIMEOUT", "").strip()
    try:
        v = float(raw) if raw else 900.0
    except ValueError:
        v = 900.0
    return v if v > 0 else 900.0


def _nvidia_target_base() -> str:
    """Куда форвардим: X-Nvidia-Base-Url > $NVIDIA_BASE_URL > default."""
    base = (
        request.headers.get("X-Nvidia-Base-Url")
        or os.environ.get("NVIDIA_BASE_URL")
        or NVIDIA_DEFAULT_BASE
    ).strip()
    return base.rstrip("/")


@app.route(
    "/nvidia/<path:subpath>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def nvidia_proxy(subpath: str):
    """Прозрачный реверс-прокси к NVIDIA NIM API.

    Поддерживает SSE-стриминг: upstream-ответ читается чанками и сразу
    отдаётся вниз по соединению, без буферизации всего тела.
    """
    # CORS-preflight закрывается flask_cors на уровне приложения. Этот хэндлер
    # тоже отвечает 204, чтобы не ходить в upstream зря.
    if request.method == "OPTIONS":
        return ("", 204)

    base = _nvidia_target_base()
    parsed = _urllib_parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({
            "error": f"Некорректный NVIDIA Base URL: {base!r}. "
                     "Ожидается абсолютный http(s) URL."
        }), 400

    sub = subpath.lstrip("/")
    target = f"{base}/{sub}" if sub else base
    if request.query_string:
        sep = "&" if "?" in target else "?"
        target = target + sep + request.query_string.decode("utf-8", errors="ignore")

    fwd_headers: dict[str, str] = {}
    for hk, hv in request.headers.items():
        if hk.lower() in _NVIDIA_PROXY_SKIP_REQ_HEADERS:
            continue
        fwd_headers[hk] = hv
    # Явно проставляем Host upstream-а — некоторые промежуточные сети ругаются
    # без него.
    fwd_headers["Host"] = parsed.netloc

    body: bytes | None = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        body = request.get_data() or None

    upstream_req = _urllib_request.Request(
        target,
        data=body,
        method=request.method,
        headers=fwd_headers,
    )

    timeout = _nvidia_proxy_timeout()
    try:
        upstream = _urllib_request.urlopen(upstream_req, timeout=timeout)
    except _urllib_error.HTTPError as exc:
        # 4xx/5xx — это НЕ ошибка прокси, а легитимный ответ NVIDIA. Тело
        # ошибки нужно отдать клиенту как есть (там JSON с подробностями).
        upstream = exc
    except _urllib_error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return jsonify({
            "error": f"NVIDIA upstream недоступен ({base}): {reason}",
        }), 502
    except (socket.timeout, TimeoutError):
        return jsonify({
            "error": f"NVIDIA upstream таймаут после {timeout:.0f}s. "
                     "Увеличьте NVIDIA_PROXY_TIMEOUT для длинных стримов.",
        }), 504
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"NVIDIA proxy error: {exc}"}), 502

    status = getattr(upstream, "status", None) or upstream.getcode() or 200

    out_headers: list[tuple[str, str]] = []
    for hk, hv in upstream.headers.items():
        if hk.lower() in _NVIDIA_PROXY_SKIP_RESP_HEADERS:
            continue
        out_headers.append((hk, hv))

    def _generate():
        try:
            while True:
                try:
                    chunk = upstream.read(8192)
                except (socket.timeout, TimeoutError):
                    # При таймауте посреди стрима просто завершаем — клиент
                    # увидит обрыв SSE и сможет переподключиться.
                    break
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return Response(
        stream_with_context(_generate()),
        status=status,
        headers=out_headers,
    )


@app.route("/nvidia", methods=["GET"])
@app.route("/nvidia/", methods=["GET"])
def nvidia_proxy_info():
    """Информация о встроенном NVIDIA-прокси для UI/диагностики."""
    return jsonify({
        "ok": True,
        "default_base": NVIDIA_DEFAULT_BASE,
        "configured_base": os.environ.get("NVIDIA_BASE_URL") or NVIDIA_DEFAULT_BASE,
        "timeout_seconds": _nvidia_proxy_timeout(),
        "hint": "POST /nvidia/chat/completions с заголовком "
                "Authorization: Bearer nvapi-... — прокси форвардит запрос "
                "и SSE-ответ в NVIDIA NIM.",
    })


# ═══════════════════════ /ollama/* — реверс-прокси к Ollama ═════════════════
#
# Ollama сервер (по умолчанию http://localhost:11434) отдаёт CORS-заголовки
# только если поднять его с `OLLAMA_ORIGINS=*`. Чтобы пользователю не
# приходилось перенастраивать systemd-сервис / launchctl — повторяем тот же
# трюк, что и для NVIDIA: проксируем запросы через wsapi (тот же origin).
#
# URL-схема:
#     /ollama/v1/chat/completions   → ${BASE}/v1/chat/completions  (OpenAI-compat chat)
#     /ollama/v1/models             → ${BASE}/v1/models
#     /ollama/api/tags              → ${BASE}/api/tags             (native list)
#     /ollama/api/pull              → ${BASE}/api/pull             (ndjson stream)
#     /ollama/api/version           → ${BASE}/api/version
# где BASE = X-Ollama-Base-Url > $OLLAMA_BASE_URL > http://localhost:11434.

OLLAMA_DEFAULT_BASE = "http://localhost:11434"

_OLLAMA_PROXY_SKIP_REQ_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "x-ollama-base-url",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "origin",
    "referer",
    "cookie",
})

_OLLAMA_PROXY_SKIP_RESP_HEADERS = frozenset({
    "connection",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    # flask_cors сам выставит CORS-заголовки.
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-expose-headers",
})


def _ollama_proxy_timeout() -> float:
    raw = os.environ.get("OLLAMA_PROXY_TIMEOUT", "").strip()
    try:
        v = float(raw) if raw else 900.0
    except ValueError:
        v = 900.0
    return v if v > 0 else 900.0


def _ollama_target_base() -> str:
    """Куда форвардим: X-Ollama-Base-Url > $OLLAMA_BASE_URL > default."""
    base = (
        request.headers.get("X-Ollama-Base-Url")
        or os.environ.get("OLLAMA_BASE_URL")
        or OLLAMA_DEFAULT_BASE
    ).strip()
    return base.rstrip("/")


@app.route(
    "/ollama/<path:subpath>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def ollama_proxy(subpath: str):
    """Прозрачный реверс-прокси к Ollama (Local & Cloud).

    Поддерживает SSE/ndjson-стриминг: upstream-ответ читается чанками и сразу
    отдаётся вниз по соединению, без буферизации всего тела (важно для
    /api/pull и /v1/chat/completions со stream=true).
    """
    if request.method == "OPTIONS":
        return ("", 204)

    base = _ollama_target_base()
    parsed = _urllib_parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({
            "error": f"Некорректный Ollama Base URL: {base!r}. "
                     "Ожидается абсолютный http(s) URL."
        }), 400

    sub = subpath.lstrip("/")
    target = f"{base}/{sub}" if sub else base
    if request.query_string:
        sep = "&" if "?" in target else "?"
        target = target + sep + request.query_string.decode("utf-8", errors="ignore")

    fwd_headers: dict[str, str] = {}
    for hk, hv in request.headers.items():
        if hk.lower() in _OLLAMA_PROXY_SKIP_REQ_HEADERS:
            continue
        fwd_headers[hk] = hv
    fwd_headers["Host"] = parsed.netloc

    body: bytes | None = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        body = request.get_data() or None

    upstream_req = _urllib_request.Request(
        target,
        data=body,
        method=request.method,
        headers=fwd_headers,
    )

    timeout = _ollama_proxy_timeout()
    try:
        upstream = _urllib_request.urlopen(upstream_req, timeout=timeout)
    except _urllib_error.HTTPError as exc:
        upstream = exc
    except _urllib_error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return jsonify({
            "error": f"Ollama upstream недоступен ({base}): {reason}",
        }), 502
    except (socket.timeout, TimeoutError):
        return jsonify({
            "error": f"Ollama upstream таймаут после {timeout:.0f}s. "
                     "Увеличьте OLLAMA_PROXY_TIMEOUT для длинных стримов / pull.",
        }), 504
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"Ollama proxy error: {exc}"}), 502

    status = getattr(upstream, "status", None) or upstream.getcode() or 200

    out_headers: list[tuple[str, str]] = []
    for hk, hv in upstream.headers.items():
        if hk.lower() in _OLLAMA_PROXY_SKIP_RESP_HEADERS:
            continue
        out_headers.append((hk, hv))

    def _generate():
        try:
            while True:
                try:
                    chunk = upstream.read(8192)
                except (socket.timeout, TimeoutError):
                    break
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return Response(
        stream_with_context(_generate()),
        status=status,
        headers=out_headers,
    )


@app.route("/ollama", methods=["GET"])
@app.route("/ollama/", methods=["GET"])
def ollama_proxy_info():
    """Информация о встроенном Ollama-прокси для UI/диагностики."""
    return jsonify({
        "ok": True,
        "default_base": OLLAMA_DEFAULT_BASE,
        "configured_base": os.environ.get("OLLAMA_BASE_URL") or OLLAMA_DEFAULT_BASE,
        "timeout_seconds": _ollama_proxy_timeout(),
        "hint": "POST /ollama/v1/chat/completions с заголовком "
                "X-Ollama-Base-Url: http://host:11434 — прокси форвардит "
                "запрос и SSE-ответ в Ollama (Local или Cloud).",
    })


# ═══════════════════════ /mem/* — память (БД-бэкенд для localStorage) ═══════
#
# Сюда переезжают чаты/скилы/MCP/настройки, которые раньше жили только в
# браузерном localStorage. Подробности — в memory_db.py.

@app.route("/mem/health")
def mem_health():
    try:
        return jsonify(memory_db().health())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/mem/export")
def mem_export():
    return jsonify(memory_db().export_all())


@app.route("/mem/import", methods=["POST"])
def mem_import():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400, description="Ожидался JSON-объект (localStorage shape или /mem/export shape)")
    try:
        return jsonify({"ok": True, **memory_db().import_all(payload)})
    except ValueError as e:
        abort(400, description=str(e))


@app.route("/mem/sync", methods=["POST"])
def mem_sync():
    """Инкрементальный апдейт: `{set: {key:value}, delete: [keys]}`."""
    payload = request.get_json(silent=True) or {}
    sets = payload.get("set") if isinstance(payload.get("set"), dict) else {}
    deletes = payload.get("delete") if isinstance(payload.get("delete"), list) else []
    return jsonify(memory_db().sync(sets, deletes))


@app.route("/mem/reset", methods=["POST"])
def mem_reset():
    memory_db().reset()
    return jsonify({"ok": True, **memory_db().health()})


# ── kv ──

@app.route("/mem/kv", methods=["GET"])
def mem_kv_get():
    key = request.args.get("key", "")
    if not key:
        return jsonify(memory_db().kv_all())
    val = memory_db().kv_get(key)
    return jsonify({"key": key, "value": val})


@app.route("/mem/kv", methods=["POST"])
def mem_kv_set():
    payload = request.get_json(silent=True) or {}
    key = (payload.get("key") or "").strip()
    if not key:
        abort(400, description="key обязателен")
    value = payload.get("value")
    if value is None:
        memory_db().kv_delete(key)
        return jsonify({"ok": True, "deleted": True, "key": key})
    memory_db().kv_set(key, str(value))
    return jsonify({"ok": True, "key": key})


@app.route("/mem/kv", methods=["DELETE"])
def mem_kv_delete():
    key = request.args.get("key", "")
    if not key:
        abort(400, description="key обязателен")
    deleted = memory_db().kv_delete(key)
    return jsonify({"ok": True, "deleted": deleted, "key": key})


# ── chats ──

@app.route("/mem/chats", methods=["GET"])
def mem_chats_list():
    return jsonify({"chats": memory_db().chats_list()})


@app.route("/mem/chats/<chat_id>", methods=["GET"])
def mem_chat_get(chat_id: str):
    chat = memory_db().chats_get(chat_id)
    if not chat:
        abort(404, description="Чат не найден")
    return jsonify(chat)


@app.route("/mem/chats", methods=["POST"])
def mem_chat_upsert():
    payload = request.get_json(silent=True) or {}
    if not payload.get("id"):
        abort(400, description="id обязателен")
    return jsonify(memory_db().chats_upsert(payload))


@app.route("/mem/chats/<chat_id>", methods=["DELETE"])
def mem_chat_delete(chat_id: str):
    deleted = memory_db().chats_delete(chat_id)
    return jsonify({"ok": True, "deleted": deleted, "id": chat_id})


# ── skills ──

@app.route("/mem/skills", methods=["GET"])
def mem_skills_list():
    return jsonify({"skills": memory_db().skills_list()})


@app.route("/mem/skills", methods=["POST"])
def mem_skills_replace():
    """Полная замена набора скилов."""
    payload = request.get_json(silent=True) or {}
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if skills is None and isinstance(payload, list):
        skills = payload
    if not isinstance(skills, list):
        abort(400, description="Ожидался массив `skills` (или массив верхнего уровня)")
    memory_db().skills_replace_all(skills)
    return jsonify({"ok": True, "skills": memory_db().skills_list()})


@app.route("/mem/skills/<skill_id>", methods=["DELETE"])
def mem_skill_delete(skill_id: str):
    deleted = memory_db().skills_delete(skill_id)
    return jsonify({"ok": True, "deleted": deleted, "id": skill_id})


# ── mcp_servers ──

@app.route("/mem/mcp/servers", methods=["GET"])
def mem_mcp_list():
    return jsonify({"servers": memory_db().mcp_list()})


@app.route("/mem/mcp/servers", methods=["POST"])
def mem_mcp_replace():
    payload = request.get_json(silent=True) or {}
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if servers is None and isinstance(payload, list):
        servers = payload
    if not isinstance(servers, list):
        abort(400, description="Ожидался массив `servers` (или массив верхнего уровня)")
    memory_db().mcp_replace_all(servers)
    return jsonify({"ok": True, "servers": memory_db().mcp_list()})


@app.route("/mem/mcp/servers/<server_id>", methods=["DELETE"])
def mem_mcp_delete(server_id: str):
    deleted = memory_db().mcp_delete(server_id)
    return jsonify({"ok": True, "deleted": deleted, "id": server_id})


# ═══════════════════════ /sys/* — самообновление из GitHub ═══════════════════════

# Корень установки определяется по расположению самого wsapi_server.py:
# скрипт лежит в корне репозитория Agent Pro.
INSTALL_DIR: str = os.path.dirname(os.path.abspath(__file__))


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Запустить git с явным cwd. Возвращает (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git не установлен"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} превысил таймаут {timeout}s"


def _git_repo_root(start: str) -> str | None:
    """Найти корень git-репо для каталога `start`. None — если это не git."""
    code, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=start, timeout=5)
    if code == 0 and out:
        return out
    return None


def _git_status(repo: str) -> dict:
    """Собрать описание состояния репо: ветка, коммит, dirty, origin URL."""
    info: dict = {"repo": repo, "is_git": True}

    code, branch, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=5)
    info["branch"] = branch if code == 0 else None

    code, sha, _ = _run_git(["rev-parse", "HEAD"], cwd=repo, timeout=5)
    info["sha"] = sha if code == 0 else None
    info["short_sha"] = sha[:7] if code == 0 and sha else None

    code, log, _ = _run_git(
        ["log", "-1", "--pretty=format:%h%x09%s%x09%cI%x09%an"],
        cwd=repo,
        timeout=5,
    )
    if code == 0 and log:
        parts = log.split("\t")
        info["last_commit"] = {
            "short_sha": parts[0] if len(parts) > 0 else None,
            "subject": parts[1] if len(parts) > 1 else None,
            "date": parts[2] if len(parts) > 2 else None,
            "author": parts[3] if len(parts) > 3 else None,
        }
    else:
        info["last_commit"] = None

    code, status, _ = _run_git(["status", "--porcelain"], cwd=repo, timeout=10)
    info["dirty"] = bool(status) if code == 0 else None

    code, origin, _ = _run_git(["config", "--get", "remote.origin.url"], cwd=repo, timeout=5)
    info["origin_url"] = origin if code == 0 and origin else None

    return info


def _commit_list(repo: str, range_spec: str, limit: int = 20) -> list[dict]:
    """Краткий список коммитов в диапазоне (например, HEAD..origin/main)."""
    code, out, _ = _run_git(
        ["log", range_spec, f"-{limit}", "--pretty=format:%h%x09%s%x09%cI%x09%an"],
        cwd=repo,
        timeout=10,
    )
    if code != 0 or not out:
        return []
    commits: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        commits.append(
            {
                "short_sha": parts[0] if len(parts) > 0 else None,
                "subject": parts[1] if len(parts) > 1 else None,
                "date": parts[2] if len(parts) > 2 else None,
                "author": parts[3] if len(parts) > 3 else None,
            }
        )
    return commits


@app.route("/sys/version")
def sys_version():
    """Текущее состояние установки."""
    repo = _git_repo_root(INSTALL_DIR)
    if not repo:
        return jsonify({
            "ok": True,
            "is_git": False,
            "install_dir": INSTALL_DIR,
            "error": "Каталог установки не является git-репозиторием — обновление через UI недоступно.",
        })
    return jsonify({"ok": True, "install_dir": INSTALL_DIR, **_git_status(repo)})


@app.route("/sys/update/check")
def sys_update_check():
    """git fetch + проверка отставания от удалённой ветки."""
    repo = _git_repo_root(INSTALL_DIR)
    if not repo:
        return jsonify({
            "ok": False,
            "is_git": False,
            "install_dir": INSTALL_DIR,
            "error": "Каталог установки не является git-репозиторием.",
        }), 200

    status = _git_status(repo)
    branch = status.get("branch")
    if not branch or branch == "HEAD":
        return jsonify({
            "ok": False,
            **status,
            "error": "Нет активной ветки (detached HEAD). Переключитесь на ветку (например, main) и попробуйте снова.",
        }), 200

    code, _, fetch_err = _run_git(["fetch", "--tags", "origin", branch], cwd=repo, timeout=60)
    if code != 0:
        return jsonify({
            "ok": False,
            **status,
            "error": f"git fetch упал: {fetch_err or 'неизвестная ошибка'}",
        }), 200

    code, counts, _ = _run_git(
        ["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"],
        cwd=repo,
        timeout=10,
    )
    ahead = behind = 0
    if code == 0 and counts:
        try:
            a, b = counts.split()
            ahead, behind = int(a), int(b)
        except ValueError:
            pass

    code, remote_sha, _ = _run_git(["rev-parse", f"origin/{branch}"], cwd=repo, timeout=5)
    remote_short = remote_sha[:7] if code == 0 and remote_sha else None

    incoming = _commit_list(repo, f"HEAD..origin/{branch}", limit=20) if behind > 0 else []

    return jsonify({
        "ok": True,
        **status,
        "remote_branch": f"origin/{branch}",
        "remote_sha": remote_sha if code == 0 else None,
        "remote_short_sha": remote_short,
        "ahead": ahead,
        "behind": behind,
        "update_available": behind > 0,
        "incoming_commits": incoming,
    })


def _run_cmd(args: list[str], cwd: str, timeout: int = 600) -> dict:
    """Запустить произвольную команду и вернуть структурированный лог."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "cmd": " ".join(args),
            "code": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
            "ok": proc.returncode == 0,
        }
    except FileNotFoundError:
        return {"cmd": " ".join(args), "code": 127, "stdout": "", "stderr": f"{args[0]} не установлен", "ok": False}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(args), "code": 124, "stdout": "", "stderr": f"таймаут {timeout}s", "ok": False}


@app.route("/sys/update/apply", methods=["POST"])
def sys_update_apply():
    """git pull --ff-only + опциональная переустановка зависимостей."""
    repo = _git_repo_root(INSTALL_DIR)
    if not repo:
        return jsonify({
            "ok": False,
            "is_git": False,
            "install_dir": INSTALL_DIR,
            "error": "Каталог установки не является git-репозиторием.",
        }), 200

    data = request.get_json(silent=True) or {}
    install_deps = bool(data.get("install_deps", True))

    pre = _git_status(repo)
    branch = pre.get("branch")
    if not branch or branch == "HEAD":
        return jsonify({
            "ok": False,
            **pre,
            "error": "Нет активной ветки (detached HEAD).",
        }), 200
    if pre.get("dirty"):
        return jsonify({
            "ok": False,
            **pre,
            "error": "В рабочем каталоге есть незакоммиченные изменения. Закоммитьте или отмените их и повторите.",
        }), 200

    logs: list[dict] = []
    pre_sha = pre.get("sha")

    fetch = _run_cmd(["git", "fetch", "--tags", "origin", branch], cwd=repo, timeout=120)
    logs.append(fetch)
    if not fetch["ok"]:
        return jsonify({"ok": False, **pre, "logs": logs, "error": "git fetch упал"}), 200

    pull = _run_cmd(["git", "pull", "--ff-only", "origin", branch], cwd=repo, timeout=180)
    logs.append(pull)
    if not pull["ok"]:
        return jsonify({
            "ok": False,
            **pre,
            "logs": logs,
            "error": (
                "git pull --ff-only не удался. Скорее всего, локальная ветка ушла в сторону "
                "от origin (есть локальные коммиты). Сделайте pull / rebase вручную."
            ),
        }), 200

    post = _git_status(repo)
    post_sha = post.get("sha")
    applied = []
    if pre_sha and post_sha and pre_sha != post_sha:
        applied = _commit_list(repo, f"{pre_sha}..{post_sha}", limit=50)

    # Что поменялось → решаем, надо ли ставить зависимости.
    changed_files: list[str] = []
    if pre_sha and post_sha and pre_sha != post_sha:
        code, diff, _ = _run_git(
            ["diff", "--name-only", f"{pre_sha}..{post_sha}"],
            cwd=repo,
            timeout=10,
        )
        if code == 0 and diff:
            changed_files = diff.splitlines()

    deps_changed = {
        "requirements.txt": "requirements.txt" in changed_files,
        "package.json": "package.json" in changed_files,
        "package-lock.json": "package-lock.json" in changed_files,
        "bridge/package.json": "bridge/package.json" in changed_files,
    }

    if install_deps and deps_changed["requirements.txt"]:
        # Используем тот же python, под которым крутится сервер — чтобы ставить
        # в правильный venv без угадываний пути.
        logs.append(_run_cmd(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=repo,
            timeout=600,
        ))
    if install_deps and (deps_changed["package.json"] or deps_changed["package-lock.json"]):
        logs.append(_run_cmd(["npm", "install"], cwd=repo, timeout=600))
    if install_deps and deps_changed["bridge/package.json"]:
        logs.append(_run_cmd(["npm", "install"], cwd=os.path.join(repo, "bridge"), timeout=600))

    return jsonify({
        "ok": True,
        **post,
        "previous_sha": pre_sha,
        "applied_commits": applied,
        "changed_files": changed_files,
        "deps_installed": install_deps,
        "deps_changed": deps_changed,
        "restart_required": bool(applied),
        "logs": logs,
    })


# ═══════════════════════ entrypoint ═══════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Workspace API — файловый бэкенд для Agent Pro.")
    parser.add_argument("--workspace", "-w", help="Путь к рабочей области (переопределяет $WORKSPACE_DIR).")
    parser.add_argument("--host", default=os.environ.get("WORKSPACE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKSPACE_PORT", "8764")))
    parser.add_argument("--allowed-roots", help="':'-разделённый список разрешённых корней (переопределяет $WORKSPACE_ROOTS).")
    # ── TLS ──────────────────────────────────────────────────────────────────
    # Когда run.py поднимает стек по HTTPS, он передаёт сюда пути к сертификату
    # и ключу. При прямом запуске wsapi_server.py отдельно — можно не указывать
    # (или подложить свои), тогда работаем по чистому HTTP.
    parser.add_argument("--cert", default=os.environ.get("AGENT_PRO_TLS_CERT") or None,
                        help="PEM-сертификат для HTTPS (вместе с --key).")
    parser.add_argument("--key", default=os.environ.get("AGENT_PRO_TLS_KEY") or None,
                        help="PEM-ключ для HTTPS (вместе с --cert).")
    args = parser.parse_args()

    global BASE_DIR, ALLOWED_ROOTS
    if args.workspace:
        BASE_DIR = os.path.abspath(os.path.expanduser(args.workspace))
        os.makedirs(BASE_DIR, exist_ok=True)
    if args.allowed_roots:
        ALLOWED_ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in args.allowed_roots.split(":") if p.strip()]

    ssl_context = None
    scheme = "http"
    if args.cert and args.key:
        if not (os.path.isfile(args.cert) and os.path.isfile(args.key)):
            raise SystemExit(
                f"--cert/--key указаны, но файлы не найдены: cert={args.cert} key={args.key}"
            )
        ssl_context = (args.cert, args.key)
        scheme = "https"

    print(f"● Workspace API запущен на {scheme}://{args.host}:{args.port}")
    print(f"  Рабочая область : {BASE_DIR}")
    print(f"  Разрешённые корни: {ALLOWED_ROOTS}")
    if ssl_context is not None:
        print(f"  TLS             : cert={args.cert} key={args.key}")
    try:
        mem_path = memory_db().path
        print(f"  БД памяти       : {mem_path}")
    except Exception as e:
        print(f"  БД памяти       : недоступна ({e})")
    app.run(host=args.host, port=args.port, debug=False, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
