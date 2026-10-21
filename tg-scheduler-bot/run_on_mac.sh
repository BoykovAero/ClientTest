#!/usr/bin/env bash
# Запуск бота на своём Mac — без сервера и без карт.
#
#   cd ~/путь/к/tg-scheduler-bot
#   ./run_on_mac.sh
#
# Ставит зависимости и заводит службу launchd: бот стартует при входе в
# систему и сам перезапускается после сбоя. Повторный запуск = обновление.
# Для Linux-сервера есть отдельный deploy.sh — этот скрипт только для macOS.

set -Eeuo pipefail

LABEL="com.tg-scheduler-bot"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
VENV="${SRC_DIR}/.venv"
STATE_DIR="${SRC_DIR}/data"
LOG="${STATE_DIR}/bot.log"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ─── Проверки до любых изменений ─────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || die "Это скрипт для macOS. Для Linux-сервера — deploy.sh."
command -v python3 >/dev/null || die \
    "Нет python3. Установи инструменты разработчика: xcode-select --install"

[[ -f "${SRC_DIR}/.env" ]] || die \
    "Нет файла .env в ${SRC_DIR}. Собери его: python3 setup_env.py (DEPLOY.md, шаги 1–4)."
grep -q '^TELEGRAM_BOT_TOKEN=.\+' "${SRC_DIR}/.env" || die "В .env пуст TELEGRAM_BOT_TOKEN."
grep -Eq '^ALLOWED_USER_ID="?[0-9]+"?' "${SRC_DIR}/.env" || die "В .env пуст или нечисловой ALLOWED_USER_ID."
grep -q '^OPENAI_API_KEY=.\+' "${SRC_DIR}/.env" || die "В .env пуст OPENAI_API_KEY."

# Google: либо ключ сервисного аккаунта в .env, либо пара файлов OAuth.
if grep -q '^GOOGLE_SERVICE_ACCOUNT_JSON=.\+' "${SRC_DIR}/.env" \
   || grep -q '^GOOGLE_SERVICE_ACCOUNT_FILE=.\+' "${SRC_DIR}/.env"; then
    log "Google: ключ сервисного аккаунта из .env"
else
    for f in credentials.json token.json; do
        [[ -f "${SRC_DIR}/${f}" ]] || die \
            "Нет ${f}, и в .env не задан GOOGLE_SERVICE_ACCOUNT_JSON. Нужно одно из двух (DEPLOY.md, шаг 4)."
    done
    log "Google: OAuth-файлы credentials.json token.json"
fi

# ─── Зависимости ─────────────────────────────────────────────────────────────
if [[ ! -x "${VENV}/bin/python" ]]; then
    log "Создаю окружение Python"
    python3 -m venv "${VENV}"
fi
log "Ставлю зависимости (может занять пару минут)"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r "${SRC_DIR}/requirements.txt"

mkdir -p "${STATE_DIR}"

# ─── Служба launchd ──────────────────────────────────────────────────────────
log "Записываю службу ${PLIST}"
mkdir -p "${HOME}/Library/LaunchAgents"
cat > "${PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV}/bin/python</string>
        <string>-m</string>
        <string>bot</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SRC_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
</dict>
</plist>
PLIST_EOF

# ─── Запуск ──────────────────────────────────────────────────────────────────
GUI="gui/$(id -u)"
# Снимаем прежнюю версию службы, если была, и поднимаем заново.
launchctl bootout "${GUI}/${LABEL}" 2>/dev/null || true
if ! launchctl bootstrap "${GUI}" "${PLIST}" 2>/dev/null; then
    # На старых macOS bootstrap может отсутствовать — пробуем по-старому.
    launchctl load -w "${PLIST}"
fi
launchctl enable "${GUI}/${LABEL}" 2>/dev/null || true
launchctl kickstart -k "${GUI}/${LABEL}" 2>/dev/null || true

sleep 3
if launchctl print "${GUI}/${LABEL}" >/dev/null 2>&1; then
    log "Служба поднята. Бот стартует при входе и перезапускается сам."
    echo
    log "Логи в реальном времени:  tail -f \"${LOG}\""
    log "Остановить:               launchctl bootout ${GUI}/${LABEL}"
    log "Запустить снова:          ./run_on_mac.sh"
    echo
    log "Последние строки лога:"
    [[ -f "${LOG}" ]] && tail -n 15 "${LOG}" || echo "  (лог ещё пуст, подожди несколько секунд)"
else
    warn "Служба не поднялась. Последние строки лога:"
    [[ -f "${LOG}" ]] && tail -n 40 "${LOG}"
    exit 1
fi
