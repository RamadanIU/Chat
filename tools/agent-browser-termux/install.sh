#!/usr/bin/env bash
# One-command installer for agent-browser-termux shim.
#
# Usage (Termux):
#   curl -sL https://raw.githubusercontent.com/RamadanIU/Chat/main/tools/agent-browser-termux/install.sh | bash
#
# Or, if you've already cloned the repo:
#   bash tools/agent-browser-termux/install.sh
#
# The script:
#   1. Verifies it's running under Termux (or any Linux with Node + Chromium).
#   2. Installs nodejs/git/curl if missing (Termux: pkg install).
#   3. Ensures ~/playwright-termux exists with playwright-core installed.
#   4. Copies daemon.js and cli.js into ~/playwright-termux/agent-browser-shim/.
#   5. Drops an `agent-browser` wrapper into $PREFIX/bin (Termux) or ~/.local/bin.
#   6. Smoke-tests `agent-browser version`.

set -euo pipefail

color() { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
info() { color '1;34' "==> $*"; }
warn() { color '1;33' "!!  $*"; }
err()  { color '1;31' "xx  $*" 1>&2; }

# ── Detect environment ──────────────────────────────────────────────────────
if [ -n "${PREFIX:-}" ] && [ -d "${PREFIX}" ] && [ -d "/data/data/com.termux" ]; then
  IS_TERMUX=1
  BIN_DIR="${PREFIX}/bin"
else
  IS_TERMUX=0
  BIN_DIR="${HOME}/.local/bin"
fi

PT_ROOT="${PLAYWRIGHT_TERMUX_ROOT:-${HOME}/playwright-termux}"
SHIM_DIR="${PT_ROOT}/agent-browser-shim"

# Source: prefer this script's own directory if running from a clone; else fetch from GitHub
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/daemon.js" ] && [ -f "${SCRIPT_DIR}/cli.js" ]; then
  SOURCE_MODE="local"
else
  SOURCE_MODE="remote"
fi

info "Installing agent-browser-termux shim"
info "  termux:        $IS_TERMUX"
info "  playwright:    ${PT_ROOT}"
info "  shim:          ${SHIM_DIR}"
info "  bin:           ${BIN_DIR}"
info "  source:        ${SOURCE_MODE}"

# ── Install prerequisites ───────────────────────────────────────────────────
need_install() { ! command -v "$1" >/dev/null 2>&1; }

if [ $IS_TERMUX -eq 1 ]; then
  PKGS=()
  need_install node && PKGS+=(nodejs)
  need_install git  && PKGS+=(git)
  need_install curl && PKGS+=(curl)
  if [ ${#PKGS[@]} -gt 0 ]; then
    info "Installing termux packages: ${PKGS[*]}"
    pkg install -y "${PKGS[@]}"
  fi
  # Chromium: we DO NOT auto-install it because the user already has it via
  # the existing playwright-termux setup. If CHROMIUM_PATH is wrong, daemon
  # will say so loudly when starting.
fi

if need_install node; then
  err "node is required but not installed."
  exit 1
fi

# ── Ensure playwright-termux project ────────────────────────────────────────
if [ ! -d "${PT_ROOT}" ]; then
  info "Creating ${PT_ROOT}"
  mkdir -p "${PT_ROOT}"
  cat > "${PT_ROOT}/package.json" <<'JSON'
{
  "name": "playwright-termux",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "dotenv": "^16.4.5",
    "playwright-core": "^1.54.1"
  }
}
JSON
fi

if [ ! -f "${PT_ROOT}/.env" ]; then
  warn "${PT_ROOT}/.env not found. Trying to detect Chromium binary..."
  CANDIDATE=""
  for p in \
    "${PREFIX:-/usr}/bin/chromium-browser" \
    "${PREFIX:-/usr}/bin/chromium" \
    "/usr/bin/chromium" \
    "/usr/bin/chromium-browser" \
    "/usr/bin/google-chrome" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)" \
    "$(command -v google-chrome 2>/dev/null || true)"
  do
    if [ -n "$p" ] && [ -x "$p" ]; then CANDIDATE="$p"; break; fi
  done
  if [ -n "$CANDIDATE" ]; then
    info "Found Chromium at $CANDIDATE — writing .env"
    printf 'CHROMIUM_PATH=%s\n' "$CANDIDATE" > "${PT_ROOT}/.env"
  else
    warn "No Chromium found. Install it (e.g. \`pkg install chromium-browser\`) and edit ${PT_ROOT}/.env"
    printf 'CHROMIUM_PATH=/path/to/chromium\n' > "${PT_ROOT}/.env"
  fi
fi

if [ ! -d "${PT_ROOT}/node_modules/playwright-core" ]; then
  info "Installing playwright-core into ${PT_ROOT}"
  ( cd "${PT_ROOT}" && npm install --no-audit --no-fund --silent playwright-core dotenv )
fi

# ── Drop daemon.js / cli.js ─────────────────────────────────────────────────
mkdir -p "${SHIM_DIR}"

if [ "${SOURCE_MODE}" = "local" ]; then
  cp "${SCRIPT_DIR}/daemon.js" "${SHIM_DIR}/daemon.js"
  cp "${SCRIPT_DIR}/cli.js"    "${SHIM_DIR}/cli.js"
else
  RAW="https://raw.githubusercontent.com/RamadanIU/Chat/main/tools/agent-browser-termux"
  info "Downloading daemon.js / cli.js from ${RAW}"
  curl -fsSL "${RAW}/daemon.js" -o "${SHIM_DIR}/daemon.js"
  curl -fsSL "${RAW}/cli.js"    -o "${SHIM_DIR}/cli.js"
fi
chmod +x "${SHIM_DIR}/daemon.js" "${SHIM_DIR}/cli.js"

# ── Drop wrapper into bin dir ───────────────────────────────────────────────
mkdir -p "${BIN_DIR}"
WRAPPER="${BIN_DIR}/agent-browser"
cat > "${WRAPPER}" <<EOF
#!/usr/bin/env bash
# agent-browser wrapper (Termux Playwright shim)
exec node "${SHIM_DIR}/cli.js" "\$@"
EOF
chmod +x "${WRAPPER}"
info "Installed wrapper: ${WRAPPER}"

# Add ~/.local/bin to PATH for non-Termux setups
if [ $IS_TERMUX -eq 0 ]; then
  case ":$PATH:" in
    *":${BIN_DIR}:"*) ;;
    *)
      warn "${BIN_DIR} is not in your PATH. Add this to ~/.bashrc or ~/.zshrc:"
      printf '    export PATH="%s:$PATH"\n' "${BIN_DIR}"
      ;;
  esac
fi

# ── Smoke test ──────────────────────────────────────────────────────────────
info "Running smoke test: agent-browser version"
if "${WRAPPER}" version 2>&1 | head -3 ; then
  info "Done. Try:"
  info "  agent-browser open https://example.com"
  info "  agent-browser screenshot ~/screen.png"
  info "  agent-browser snapshot -i"
  info "  agent-browser kill   # to stop the daemon"
else
  err "Smoke test failed. Check ${LOG_FILE:-${HOME}/.cache/agent-browser/daemon.log}"
  exit 1
fi
