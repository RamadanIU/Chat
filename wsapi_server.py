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
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
from typing import Iterable

from flask import Flask, abort, jsonify, request
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


# ═══════════════════════ entrypoint ═══════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Workspace API — файловый бэкенд для Agent Pro.")
    parser.add_argument("--workspace", "-w", help="Путь к рабочей области (переопределяет $WORKSPACE_DIR).")
    parser.add_argument("--host", default=os.environ.get("WORKSPACE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WORKSPACE_PORT", "8764")))
    parser.add_argument("--allowed-roots", help="':'-разделённый список разрешённых корней (переопределяет $WORKSPACE_ROOTS).")
    args = parser.parse_args()

    global BASE_DIR, ALLOWED_ROOTS
    if args.workspace:
        BASE_DIR = os.path.abspath(os.path.expanduser(args.workspace))
        os.makedirs(BASE_DIR, exist_ok=True)
    if args.allowed_roots:
        ALLOWED_ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in args.allowed_roots.split(":") if p.strip()]

    print(f"● Workspace API запущен на http://{args.host}:{args.port}")
    print(f"  Рабочая область : {BASE_DIR}")
    print(f"  Разрешённые корни: {ALLOWED_ROOTS}")
    try:
        mem_path = memory_db().path
        print(f"  БД памяти       : {mem_path}")
    except Exception as e:
        print(f"  БД памяти       : недоступна ({e})")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
