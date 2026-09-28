#!/usr/bin/env bash
#
# Разворачивает бота на сервере как systemd-сервис.
#
# Запускать НА СЕРВЕРЕ, из каталога с исходниками, под sudo:
#     sudo ./deploy.sh
#
# Скрипт идемпотентен: повторный запуск = обновление кода и зависимостей.
# Переопределяемые переменные:
#     APP_USER=botuser APP_DIR=/opt/tg-scheduler-bot sudo -E ./deploy.sh

set -Eeuo pipefail

APP_NAME="tg-scheduler-bot"
APP_USER="${APP_USER:-botuser}"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_PATH="/etc/systemd/system/${APP_NAME}.service"

# Файлы с секретами: в git их нет, на сервер кладутся вручную (см. DEPLOY.md шаг 4.6)
SECRETS=(.env credentials.json token.json)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ─── Проверки до внесения любых изменений ────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Запускай под root: sudo ./deploy.sh"
command -v systemctl >/dev/null || die "systemd не найден — этот скрипт для Ubuntu/Debian."

for f in "${SECRETS[@]}"; do
    [[ -f "${SRC_DIR}/${f}" ]] || die "Нет файла ${f} в ${SRC_DIR}. Скопируй его с Mac (DEPLOY.md, шаг 4.6)."
done
[[ -f "${SRC_DIR}/requirements.txt" ]] || die "Нет requirements.txt в ${SRC_DIR}."
[[ -d "${SRC_DIR}/bot" || -f "${SRC_DIR}/bot.py" ]] || \
    die "Нет кода бота (каталога bot/ или bot.py) в ${SRC_DIR}. Деплоить нечего."

grep -q '^TELEGRAM_BOT_TOKEN=.\+' "${SRC_DIR}/.env" || die "В .env пуст TELEGRAM_BOT_TOKEN."
grep -q '^ALLOWED_USER_ID=[0-9]\+' "${SRC_DIR}/.env"  || die "В .env пуст или нечисловой ALLOWED_USER_ID."
grep -q '^OPENAI_API_KEY=.\+' "${SRC_DIR}/.env"       || die "В .env пуст OPENAI_API_KEY."

# ─── Системные пакеты ────────────────────────────────────────────────────────
log "Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync ca-certificates

# ─── Пользователь ────────────────────────────────────────────────────────────
if id -u "${APP_USER}" >/dev/null 2>&1; then
    log "Пользователь ${APP_USER} уже есть"
else
    log "Создаю системного пользователя ${APP_USER} (без права логина)"
    useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

# ─── Код ─────────────────────────────────────────────────────────────────────
log "Копирую код в ${APP_DIR}"
mkdir -p "${APP_DIR}"
if [[ "${SRC_DIR}" != "${APP_DIR}" ]]; then
    rsync -a --delete \
        --exclude 'venv/' --exclude '.venv/' --exclude '.git/' \
        --exclude '__pycache__/' --exclude '*.pyc' \
        "${SRC_DIR}/" "${APP_DIR}/"
fi

# ─── venv и зависимости ──────────────────────────────────────────────────────
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
    log "Создаю venv"
    python3 -m venv "${APP_DIR}/venv"
fi
log "Устанавливаю зависимости (может занять пару минут)"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# ─── Права ───────────────────────────────────────────────────────────────────
log "Выставляю права"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 750 "${APP_DIR}"
for f in "${SECRETS[@]}"; do
    chmod 600 "${APP_DIR}/${f}"
done

# ─── systemd-юнит ────────────────────────────────────────────────────────────
log "Собираю ${UNIT_PATH} (User=${APP_USER}, WorkingDirectory=${APP_DIR})"
sed -e "s|^User=.*|User=${APP_USER}|" \
    -e "s|^Group=.*|Group=${APP_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
    -e "s|^ExecStart=.*|ExecStart=${APP_DIR}/venv/bin/python -m bot|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=${APP_DIR}|" \
    "${APP_DIR}/${APP_NAME}.service" > "${UNIT_PATH}"

systemctl daemon-reload
log "Включаю автозапуск и стартую"
systemctl enable --now "${APP_NAME}"
systemctl restart "${APP_NAME}"

# ─── Проверка ────────────────────────────────────────────────────────────────
sleep 3
if systemctl is-active --quiet "${APP_NAME}"; then
    log "Сервис запущен. Автозапуск после перезагрузки: $(systemctl is-enabled ${APP_NAME})"
    echo
    journalctl -u "${APP_NAME}" -n 20 --no-pager
    echo
    log "Логи в реальном времени:  journalctl -u ${APP_NAME} -f"
else
    warn "Сервис не поднялся. Последние 50 строк лога:"
    journalctl -u "${APP_NAME}" -n 50 --no-pager
    exit 1
fi
