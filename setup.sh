#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — установка и запуск Agent Terminal Server
# Запустите: bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

PORT=${PORT:-8765}
TOKEN=${TOKEN:-""}

echo ""
echo "┌─────────────────────────────────────────┐"
echo "│     Agent Terminal Server — Установка   │"
echo "└─────────────────────────────────────────┘"
echo ""

# Проверяем Node.js
if ! command -v node &>/dev/null; then
  echo "❌ Node.js не найден. Установите Node.js 18+:"
  echo "   curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
  echo "   sudo apt-get install -y nodejs"
  exit 1
fi
NODE_VER=$(node -e "process.exit(parseInt(process.version.slice(1)) < 18 ? 1 : 0)" 2>&1 || echo "old")
echo "✅ Node.js: $(node -v)"

# Устанавливаем зависимости
echo ""
echo "📦 Установка зависимостей (ws, node-pty)..."
npm install --omit=dev

echo ""
echo "✅ Установка завершена!"
echo ""
echo "Запуск сервера:"
echo ""

if [ -n "$TOKEN" ]; then
  echo "  TOKEN=${TOKEN} PORT=${PORT} node server.js"
else
  echo "  node server.js"
  echo ""
  echo "  ⚠  Рекомендуем установить пароль:"
  echo "  TOKEN=мойпароль PORT=${PORT} node server.js"
fi

echo ""
echo "В настройках Agent Pro укажите:"
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "ВАШ_IP")
echo "  URL сервера:  ws://${IP}:${PORT}"
if [ -n "$TOKEN" ]; then
  echo "  Токен:        ${TOKEN}"
fi
echo ""
