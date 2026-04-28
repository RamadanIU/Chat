"""Memory DB — SQLite-хранилище для пользовательской памяти Agent Pro.

Сюда переезжает всё, что раньше лежало только в браузерном `localStorage`:
чаты, скилы, MCP-серверы и куча мелких настроек (`agent_provider`,
`agent_model`, ключи провайдеров, тема и т.д.).

Файл БД лежит в `~/.local/share/agent-pro/memory.db` (override через env
`AGENT_PRO_MEMORY_DB`). Никаких внешних зависимостей: только stdlib `sqlite3`.

Архитектура хранения:

* `kv`            — обобщённый key/value (любые `agent_*` настройки + сырой
                    дамп `mcp.servers`/`agent_chats`/`agent_custom_skills`,
                    чтобы фронт мог откатиться к localStorage без потерь).
* `chats`         — нормализованные чаты.
* `messages`      — сообщения чатов.
* `skills`        — пользовательские скилы.
* `mcp_servers`   — MCP-серверы (config + флаги).

При импорте мы заполняем ОБА слоя (kv-блобы + структурные таблицы), чтобы
БД одновременно служила и точной копией localStorage (для round-trip), и
нормальной реляционной БД (для будущих server-side фич / запросов).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator


# ─── Расположение БД ─────────────────────────────────────────────────────────

def default_db_path() -> Path:
    env = os.environ.get("AGENT_PRO_MEMORY_DB", "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    base = Path(os.path.expanduser("~/.local/share/agent-pro"))
    return base / "memory.db"


# ─── Ключи localStorage, которые мы зеркалим ─────────────────────────────────

# Ключи, чьё содержимое — JSON-массив/объект и которые получают своё
# структурное представление помимо `kv`. Источник правды — структурная
# таблица; в `kv` дублируется сырой дамп для round-trip-совместимости.
STRUCTURED_KEYS: dict[str, str] = {
    "agent_chats": "chats",
    "agent_custom_skills": "skills",
    "mcp.servers": "mcp_servers",
}

# Префиксы / точные имена ключей, которые мы считаем «памятью» (мирорим в БД).
# Всё, что не подпадает, в `kv` не уходит — иначе мы рискуем зеркалить случайный
# сторонний state, который в localStorage добавит сторонний код.
MEMORY_KEY_PREFIXES = ("agent_",)
MEMORY_KEY_EXACT = {"mcp.servers"}


def is_memory_key(key: str) -> bool:
    if not key:
        return False
    if key in MEMORY_KEY_EXACT:
        return True
    return any(key.startswith(p) for p in MEMORY_KEY_PREFIXES)


# ─── Схема ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_chats_position ON chats(position);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    position   INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, position);

CREATE TABLE IF NOT EXISTS skills (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    data       TEXT
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id             TEXT PRIMARY KEY,
    name           TEXT,
    transport      TEXT,
    url            TEXT,
    command        TEXT,
    args           TEXT,
    env            TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    disabled_tools TEXT,
    config         TEXT,
    position       INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
"""


# ─── Подключение ─────────────────────────────────────────────────────────────

class MemoryDB:
    """Тонкая обёртка над `sqlite3`. Потокобезопасна за счёт глобального лока
    (флаг `check_same_thread=False` + `threading.RLock`), потому что Flask
    легко может прийти из разных потоков."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.close()

    @contextlib.contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise

    # ── kv ──

    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )

    def kv_delete(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
        return cur.rowcount > 0

    def kv_all(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM kv").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ── chats / messages ──

    def chats_list(self) -> list[dict]:
        with self._lock:
            chats = self._conn.execute(
                "SELECT * FROM chats ORDER BY position ASC, created_at ASC"
            ).fetchall()
            msgs = self._conn.execute(
                "SELECT chat_id, position, role, content, created_at "
                "FROM messages ORDER BY chat_id ASC, position ASC"
            ).fetchall()
        by_chat: dict[str, list[dict]] = {}
        for m in msgs:
            content = _safe_json_loads(m["content"])
            by_chat.setdefault(m["chat_id"], []).append(
                _materialize_message(m["role"], content, m["created_at"])
            )
        out: list[dict] = []
        for c in chats:
            meta = _safe_json_loads(c["meta"]) if c["meta"] else {}
            base = {
                "id": c["id"],
                "name": c["name"],
                "position": c["position"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "messages": by_chat.get(c["id"], []),
            }
            if isinstance(meta, dict):
                # Любые «лишние» поля чата — кладём рядом, но не перетираем основные.
                for k, v in meta.items():
                    base.setdefault(k, v)
            out.append(base)
        return out

    def chats_get(self, chat_id: str) -> dict | None:
        for c in self.chats_list():
            if c["id"] == chat_id:
                return c
        return None

    def chats_upsert(self, chat: dict) -> dict:
        if not chat.get("id"):
            raise ValueError("chat.id required")
        now = time.time()
        chat_id = str(chat["id"])
        name = str(chat.get("name") or chat.get("title") or "Без названия")
        position = int(chat.get("position") or 0)
        meta = {k: v for k, v in chat.items() if k not in {"id", "name", "title", "position", "messages", "created_at", "updated_at"}}
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        messages = chat.get("messages") or []
        with self._txn() as cx:
            cx.execute(
                "INSERT INTO chats(id, name, position, created_at, updated_at, meta) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, position=excluded.position, "
                "  updated_at=excluded.updated_at, meta=excluded.meta",
                (chat_id, name, position, float(chat.get("created_at") or now), now, meta_json),
            )
            cx.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            for idx, msg in enumerate(messages):
                cx.execute(
                    "INSERT INTO messages(chat_id, position, role, content, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        chat_id,
                        idx,
                        str(msg.get("role") or "user"),
                        json.dumps(msg, ensure_ascii=False),
                        float(msg.get("created_at") or now),
                    ),
                )
        return self.chats_get(chat_id) or {}

    def chats_delete(self, chat_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        return cur.rowcount > 0

    def chats_replace_all(self, chats: list[dict]) -> None:
        with self._txn() as cx:
            cx.execute("DELETE FROM messages")
            cx.execute("DELETE FROM chats")
        for idx, c in enumerate(chats or []):
            payload = dict(c)
            payload.setdefault("position", idx)
            self.chats_upsert(payload)

    # ── skills ──

    def skills_list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM skills ORDER BY position ASC, created_at ASC"
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            data = _safe_json_loads(r["data"]) if r["data"] else {}
            base = {
                "id": r["id"],
                "name": r["name"],
                "content": r["content"],
                "enabled": bool(r["enabled"]),
                "position": r["position"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            if isinstance(data, dict):
                for k, v in data.items():
                    base.setdefault(k, v)
            out.append(base)
        return out

    def skills_replace_all(self, skills: list[dict]) -> None:
        now = time.time()
        with self._txn() as cx:
            cx.execute("DELETE FROM skills")
            for idx, s in enumerate(skills or []):
                sid = str(s.get("id") or f"skill_{int(now*1000)}_{idx}")
                extra = {k: v for k, v in s.items() if k not in {"id", "name", "content", "enabled", "position", "created_at", "updated_at"}}
                data_json = json.dumps(extra, ensure_ascii=False) if extra else None
                cx.execute(
                    "INSERT INTO skills(id, name, content, enabled, position, created_at, updated_at, data) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        str(s.get("name") or "Безымянный скил"),
                        str(s.get("content") or ""),
                        1 if s.get("enabled", True) else 0,
                        int(s.get("position") or idx),
                        float(s.get("created_at") or now),
                        float(s.get("updated_at") or now),
                        data_json,
                    ),
                )

    def skills_delete(self, skill_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        return cur.rowcount > 0

    # ── mcp_servers ──

    def mcp_list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM mcp_servers ORDER BY position ASC, created_at ASC"
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            cfg = _safe_json_loads(r["config"]) if r["config"] else None
            args = _safe_json_loads(r["args"]) if r["args"] else None
            env = _safe_json_loads(r["env"]) if r["env"] else None
            disabled = _safe_json_loads(r["disabled_tools"]) if r["disabled_tools"] else []
            out.append({
                "id": r["id"],
                "name": r["name"],
                "transport": r["transport"],
                "url": r["url"],
                "command": r["command"],
                "args": args or [],
                "env": env or {},
                "enabled": bool(r["enabled"]),
                "disabledTools": disabled or [],
                "config": cfg,
                "position": r["position"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
        return out

    def mcp_replace_all(self, servers: list[dict]) -> None:
        now = time.time()
        with self._txn() as cx:
            cx.execute("DELETE FROM mcp_servers")
            for idx, s in enumerate(servers or []):
                sid = str(s.get("id") or f"mcp_{int(now*1000)}_{idx}")
                cfg = s.get("config") or {}
                if isinstance(cfg, str):
                    cfg = _safe_json_loads(cfg) or {}
                if not isinstance(cfg, dict):
                    cfg = {}
                transport = cfg.get("transport") or s.get("transport")
                url = cfg.get("url") or s.get("url")
                command = cfg.get("command") or s.get("command")
                args = cfg.get("args") if isinstance(cfg.get("args"), list) else s.get("args")
                env = cfg.get("env") if isinstance(cfg.get("env"), dict) else s.get("env")
                name = cfg.get("name") or s.get("name") or sid
                disabled = s.get("disabledTools") or s.get("disabled_tools") or []
                cx.execute(
                    "INSERT INTO mcp_servers(id, name, transport, url, command, args, env, "
                    "  enabled, disabled_tools, config, position, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        name,
                        transport,
                        url,
                        command,
                        json.dumps(args, ensure_ascii=False) if args is not None else None,
                        json.dumps(env, ensure_ascii=False) if env is not None else None,
                        1 if s.get("enabled", True) else 0,
                        json.dumps(list(disabled), ensure_ascii=False) if disabled else None,
                        json.dumps(cfg, ensure_ascii=False) if cfg else None,
                        int(s.get("position") or idx),
                        float(s.get("created_at") or now),
                        float(s.get("updated_at") or now),
                    ),
                )

    def mcp_delete(self, server_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        return cur.rowcount > 0

    # ── high-level ops ──

    def health(self) -> dict:
        with self._lock:
            counts = {
                "kv": self._conn.execute("SELECT COUNT(*) AS n FROM kv").fetchone()["n"],
                "chats": self._conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"],
                "messages": self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"],
                "skills": self._conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"],
                "mcp_servers": self._conn.execute("SELECT COUNT(*) AS n FROM mcp_servers").fetchone()["n"],
            }
        size = 0
        with contextlib.suppress(OSError):
            size = self.path.stat().st_size
        return {
            "ok": True,
            "path": str(self.path),
            "size_bytes": size,
            "counts": counts,
        }

    def export_all(self) -> dict:
        return {
            "kv": self.kv_all(),
            "chats": self.chats_list(),
            "skills": self.skills_list(),
            "mcp_servers": self.mcp_list(),
        }

    def import_all(self, payload: dict) -> dict:
        """Принимает либо «localStorage shape» (dict ключей localStorage), либо
        наш `/mem/export`-shape (`{kv, chats, skills, mcp_servers}`). В любом
        случае атомарно заменяет содержимое БД."""
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")

        # Определяем форму. localStorage-shape — плоский dict со строковыми
        # значениями и хотя бы одним «memory key».
        is_local_storage_shape = (
            "kv" not in payload
            and "chats" not in payload
            and "skills" not in payload
            and "mcp_servers" not in payload
        )

        if is_local_storage_shape:
            local = payload
        else:
            local = dict(payload.get("kv") or {})

        # Структурированные пейлоады берём из явных ключей, иначе парсим из
        # localStorage-блобов.
        chats = payload.get("chats") if not is_local_storage_shape else None
        skills = payload.get("skills") if not is_local_storage_shape else None
        mcp_servers = payload.get("mcp_servers") if not is_local_storage_shape else None

        if chats is None and "agent_chats" in local:
            chats = _safe_json_loads(local["agent_chats"]) or []
        if skills is None and "agent_custom_skills" in local:
            skills = _safe_json_loads(local["agent_custom_skills"]) or []
        if mcp_servers is None and "mcp.servers" in local:
            mcp_servers = _safe_json_loads(local["mcp.servers"]) or []

        # Полностью перезаписываем kv-память (но только ключи, которые мы считаем
        # «памятью» — чтобы случайный сторонний state в localStorage не попадал
        # в БД).
        with self._txn() as cx:
            cx.execute("DELETE FROM kv")
            now = time.time()
            for k, v in (local or {}).items():
                if not is_memory_key(k) or v is None:
                    continue
                cx.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?)",
                    (k, str(v), now),
                )

        self.chats_replace_all(chats or [])
        self.skills_replace_all(skills or [])
        self.mcp_replace_all(mcp_servers or [])

        return self.health()

    def reset(self) -> None:
        with self._txn() as cx:
            cx.execute("DELETE FROM messages")
            cx.execute("DELETE FROM chats")
            cx.execute("DELETE FROM skills")
            cx.execute("DELETE FROM mcp_servers")
            cx.execute("DELETE FROM kv")

    def sync(self, sets: dict | None, deletes: Iterable[str] | None) -> dict:
        """Инкрементальный апдейт от фронта: точечные set/delete для тех ключей
        localStorage, которые мы считаем «памятью». Структурированные ключи
        (`agent_chats`, `agent_custom_skills`, `mcp.servers`) дополнительно
        перепарсиваются в нормализованные таблицы."""
        sets = sets or {}
        deletes = list(deletes or [])
        now = time.time()
        with self._txn() as cx:
            for k, v in sets.items():
                if not is_memory_key(k):
                    continue
                if v is None:
                    cx.execute("DELETE FROM kv WHERE key=?", (k,))
                else:
                    cx.execute(
                        "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (k, str(v), now),
                    )
            for k in deletes:
                if is_memory_key(k):
                    cx.execute("DELETE FROM kv WHERE key=?", (k,))
        # Перенормализуем «структурные» ключи, если их трогали.
        if "agent_chats" in sets:
            chats = _safe_json_loads(sets["agent_chats"]) or []
            self.chats_replace_all(chats)
        if "agent_custom_skills" in sets:
            skills = _safe_json_loads(sets["agent_custom_skills"]) or []
            self.skills_replace_all(skills)
        if "mcp.servers" in sets:
            servers = _safe_json_loads(sets["mcp.servers"]) or []
            self.mcp_replace_all(servers)
        return {"ok": True, "applied": len(sets), "deleted": len(deletes)}


# ─── helpers ─────────────────────────────────────────────────────────────────

def _safe_json_loads(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _materialize_message(role: str, content: Any, created_at: float) -> dict:
    """Сообщение хранится одной JSON-блобиной в `content`. Возвращаем фронту
    готовый dict, гарантируя как минимум `role`/`created_at`."""
    if isinstance(content, dict):
        out = dict(content)
        out.setdefault("role", role)
        out.setdefault("created_at", created_at)
        return out
    return {"role": role, "content": content, "created_at": created_at}


__all__ = ["MemoryDB", "default_db_path", "is_memory_key", "STRUCTURED_KEYS"]
