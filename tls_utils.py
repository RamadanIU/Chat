"""tls_utils.py — централизованная работа с TLS-сертификатами для всех сервисов.

Зачем: фронт + Workspace API + terminal-server + MCP bridge должны жить либо все
на HTTPS/WSS, либо все на HTTP/WS — иначе браузер режет mixed-content. Эта
утилита по запросу обеспечивает наличие cert.pem/key.pem (свои или
самоподписанные), а run.py пробрасывает пути ко всем дочерним сервисам.

Поведение:
  • Если заданы AGENT_PRO_TLS_CERT и AGENT_PRO_TLS_KEY — используем их.
  • Иначе кладём пару в ~/.cache/chat-stack/tls/{cert.pem,key.pem}
    (можно переопределить AGENT_PRO_TLS_DIR).
  • Если файлов нет — генерируем самоподписанный сертификат через `openssl`
    с SAN на localhost / 127.0.0.1 / ::1 / hostname / все локальные IPv4.

Самоподписанный сертификат вызовет в браузере «Подключение не защищено» при
первом открытии — это нормально. Чтобы убрать предупреждение:
  • либо подложите свой сертификат (Let's Encrypt / mkcert) через env-переменные,
  • либо доверьте cert.pem системе (Linux: /usr/local/share/ca-certificates,
    Android/Termux: chrome://flags + import; macOS: Keychain → Trust always).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

DEFAULT_TLS_DIR = Path.home() / ".cache" / "chat-stack" / "tls"
DEFAULT_CERT_NAME = "cert.pem"
DEFAULT_KEY_NAME = "key.pem"
SELF_SIGNED_DAYS = 3650  # 10 лет — это всё равно локальный self-signed


def _local_ipv4_addresses() -> list[str]:
    """Лучшая попытка перечислить локальные IPv4-адреса (без netifaces)."""
    addrs: set[str] = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, family=socket.AF_INET):
            addr = info[4][0]
            if addr:
                addrs.add(addr)
    except OSError:
        pass
    # Дополнительно — UDP-«фейк»-коннект к публичному IP, чтобы получить
    # адрес «исходящего» интерфейса. Без отправки реальных пакетов.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(addrs)


def _san_list() -> list[str]:
    san: list[str] = ["DNS:localhost"]
    try:
        host = socket.gethostname()
        if host and host != "localhost":
            san.append(f"DNS:{host}")
    except OSError:
        pass
    for ip in _local_ipv4_addresses():
        san.append(f"IP:{ip}")
    san.append("IP:::1")
    return san


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Сгенерировать пару cert.pem/key.pem с разумными SAN.

    Использует системный `openssl`, потому что он стандартно есть и в
    Ubuntu/Debian, и в Termux. Если openssl нет — кидаем понятную ошибку.
    """
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "openssl не найден в PATH — нужен для генерации самоподписанного "
            "TLS-сертификата. Установите его (Ubuntu/Debian: `sudo apt install "
            "openssl`; Termux: `pkg install openssl-tool`) или подложите свой "
            "сертификат через AGENT_PRO_TLS_CERT / AGENT_PRO_TLS_KEY."
        )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    san = ",".join(_san_list())
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-days", str(SELF_SIGNED_DAYS),
        "-subj", "/CN=Agent Pro Local",
        "-addext", f"subjectAltName={san}",
        "-keyout", str(key_path),
        "-out",    str(cert_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Удаляем недопеченные файлы, чтобы при следующем запуске попытаться снова.
        for p in (cert_path, key_path):
            try:
                p.unlink()
            except OSError:
                pass
        raise RuntimeError(
            f"openssl завершился с кодом {proc.returncode}. stderr:\n{proc.stderr}"
        )
    # Минимальные права на ключ.
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


def is_tls_enabled() -> bool:
    """TLS включён по умолчанию; AGENT_PRO_TLS=0/false/no/off — выключить."""
    raw = os.environ.get("AGENT_PRO_TLS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def resolve_cert_paths() -> tuple[Path, Path]:
    """Вернуть (cert, key) согласно env-переменным; по умолчанию — общий кэш."""
    env_cert = os.environ.get("AGENT_PRO_TLS_CERT", "").strip()
    env_key = os.environ.get("AGENT_PRO_TLS_KEY", "").strip()
    if env_cert and env_key:
        return Path(env_cert).expanduser(), Path(env_key).expanduser()
    base = Path(os.environ.get("AGENT_PRO_TLS_DIR", str(DEFAULT_TLS_DIR))).expanduser()
    return base / DEFAULT_CERT_NAME, base / DEFAULT_KEY_NAME


def ensure_cert() -> tuple[Path, Path]:
    """Гарантировать, что cert/key существуют; вернуть пути.

    • Если оба файла на месте — возвращаем их (даже если пользователь подложил
      свои; ничего не перегенерируем).
    • Если хоть одного нет — генерируем самоподписанную пару в дефолтной
      директории. Если пользователь явно указал AGENT_PRO_TLS_CERT/KEY и при
      этом файлов нет — не угадываем за него, а кидаем понятную ошибку.
    """
    cert_path, key_path = resolve_cert_paths()
    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path

    user_provided = bool(
        os.environ.get("AGENT_PRO_TLS_CERT") and os.environ.get("AGENT_PRO_TLS_KEY")
    )
    if user_provided:
        missing = [str(p) for p in (cert_path, key_path) if not p.is_file()]
        raise FileNotFoundError(
            "AGENT_PRO_TLS_CERT/AGENT_PRO_TLS_KEY заданы, но файлы не найдены: "
            + ", ".join(missing)
        )

    _generate_self_signed(cert_path, key_path)
    return cert_path, key_path
