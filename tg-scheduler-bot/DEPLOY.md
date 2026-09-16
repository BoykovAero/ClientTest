# ТГ-планировщик — руководство по развёртыванию

Пошаговая инструкция: от пустого места до бота, который круглосуточно живёт на
сервере, каждое утро в 08:45 (МСК) спрашивает «как пройдёт день» и раскладывает
ответ в Google Calendar и в iCloud (он же «Календарь» на Mac).

> **Прочитай сначала.** Код бота и обвязка деплоя готовы. Шаги 1–4 (ключи и
> доступы) выполняются на твоём компьютере, шаг 6 — регистрация сервера,
> шаги 7–8 — собственно установка и проверка. Как устроен код, описано в
> разделе [«Как устроен код»](#приложение-a--как-устроен-код).

## Что в этом каталоге

| Файл | Назначение | Где используется |
| --- | --- | --- |
| `DEPLOY.md` | этот документ | — |
| `bot/` | код бота (точка входа `python -m bot`) | шаг 7 |
| `tests/` | тесты на чистую логику, сеть не нужна | разработка |
| `setup_env.py` | сбор и проверка всех ключей, запись `.env` | шаги 1–4, **на Mac** |
| `.env.example` | шаблон всех переменных окружения | шаги 1–4 |
| `google_auth_setup.py` | одноразовая OAuth-авторизация Google | шаг 4, **на Mac** |
| `requirements.txt` | python-зависимости | шаг 7 |
| `tg-scheduler-bot.service` | шаблон systemd-юнита | шаг 7 |
| `deploy.sh` | автоматическая установка на сервере | шаг 7, **на сервере** |
| `.gitignore` | защита от коммита секретов | всегда |

## Главное правило про секреты

`.env`, `credentials.json` и `token.json` **никогда не попадают в git**. Они
перечислены в `.gitignore`. Проверить перед каждым пушем:

```bash
git status --short          # этих трёх файлов быть не должно
git check-ignore -v .env credentials.json token.json   # должен вывести 3 строки
```

Если секрет всё-таки утёк в коммит — не надо «удалять файл следующим коммитом».
Он останется в истории. Нужно: **отозвать ключ** (перевыпустить токен у
@BotFather, ключ в OpenAI, пароль на appleid.apple.com), и только потом чистить
историю.

---

# Шаг 1. Telegram: токен бота и твой user id

## 1.1. Токен бота

1. Открой в Telegram [@BotFather](https://t.me/BotFather) → `/start`.
2. Отправь `/newbot`.
3. **Display name** — как угодно, например `Мой планировщик`.
4. **Username** — уникальный, обязан заканчиваться на `bot`, например
   `boykov_scheduler_bot`. Если занят — BotFather скажет, придумай другой.
5. В ответ придёт строка вида `8123456789:AAH...` — это `TELEGRAM_BOT_TOKEN`.

Полезно сразу настроить меню команд: `/setcommands` → выбрать бота → вставить

```
start - проверка связи
plan - спросить про план на день прямо сейчас
today - показать, что уже записано на сегодня
```

> Токен — это полный доступ к боту. Утёк — сразу `/revoke` у BotFather.

## 1.2. Твой Telegram user id

1. Открой [@userinfobot](https://t.me/userinfobot) → `/start`.
2. Он ответит твоим числовым `Id` (например `412345678`) — это `ALLOWED_USER_ID`.

Бот обязан отвечать **только** на этот id: иначе любой, кто найдёт бота по
username, сможет писать события в твои календари и жечь твой OpenAI-баланс.

## 1.3. Записать в .env

Проще всего — интерактивным скриптом: он спросит каждый ключ из шагов 1–4,
сразу проверит его обращением к настоящему сервису и запишет `.env` с
правами 600.

```bash
python3 setup_env.py
```

Проверки не формальные: токен проверяется через `getMe`, а по твоему
`ALLOWED_USER_ID` бот шлёт пробное сообщение — если оно пришло в Telegram,
значит и id верный, и чат открыт. Скрипт можно запускать повторно: уже
введённые значения подставятся сами, вводить заново нужно только неверные.

> Перед проверкой user id открой своего бота в Telegram и нажми `/start` —
> иначе Telegram ответит «chat not found»: боты не могут писать первыми.

Руками тоже можно:

```bash
cp .env.example .env && chmod 600 .env
open -e .env        # или: nano .env
```

Значения пишутся **без кавычек и без пробелов** вокруг `=`:

```
TELEGRAM_BOT_TOKEN=8123456789:AAH...
ALLOWED_USER_ID=412345678
```

---

# Шаг 2. OpenAI API key

1. Зайди на [platform.openai.com](https://platform.openai.com/) под своим аккаунтом.
2. **Settings → Billing** — пополни баланс. Без положительного баланса ключ
   вернёт `429 insufficient_quota`, даже если у тебя есть подписка на ChatGPT:
   **подписка ChatGPT Plus и API — это разные кошельки**, Plus доступ к API не даёт.
3. **API keys → Create new secret key**. Имя: `tg-scheduler-bot`.
4. Ключ (`sk-...`) показывается **один раз** — скопируй сразу.
5. В `.env`: `OPENAI_API_KEY=sk-...`

Расход на этой задаче копеечный: одно голосовое в день через Whisper плюс один
разбор через `gpt-4o-mini` — это единицы центов в месяц. Чтобы застраховаться от
сюрпризов, в **Settings → Limits** поставь budget limit, например $5.

---

# Шаг 3. iCloud: пароль для приложения

iCloud-календарь пишется по протоколу CalDAV. Основной пароль от Apple ID для
этого не подойдёт — нужен **App-Specific Password**.

1. Убедись, что на Apple ID включена двухфакторная аутентификация (без неё
   пункт меню не появится).
2. Открой [appleid.apple.com](https://appleid.apple.com/) → войди.
3. **Sign-In and Security → App-Specific Passwords** (рус.: «Вход и безопасность»
   → «Пароли для приложений») → **+**.
4. Название: `tg-scheduler-bot`.
5. Скопируй пароль вида `abcd-efgh-ijkl-mnop` — **он показывается один раз**.
6. В `.env`:

```
ICLOUD_APPLE_ID=твоя_почта@icloud.com
ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop
ICLOUD_CALDAV_URL=https://caldav.icloud.com
ICLOUD_CALENDAR_NAME=
```

`ICLOUD_CALENDAR_NAME` оставь пустым, если пишем в календарь по умолчанию. Если
хочешь отдельный — создай его в «Календаре» на Mac (**Файл → Новый календарь →
iCloud**, назови, например, `Планировщик`) и впиши это имя. Важно, чтобы
календарь был **в разделе iCloud, а не «На моём Mac»** — локальные календари не
синхронизируются и бот их не увидит.

## 3.1. Проверить доступ, не дожидаясь бота

```bash
python3 -m venv /tmp/caldav-check && /tmp/caldav-check/bin/pip install -q caldav
/tmp/caldav-check/bin/python - <<'PY'
import caldav, os
# подставь свои значения
client = caldav.DAVClient(
    url="https://caldav.icloud.com",
    username="ТВОЙ_APPLE_ID",
    password="xxxx-xxxx-xxxx-xxxx",
)
for cal in client.principal().calendars():
    print("-", cal.name)
PY
```

Если вывелся список календарей — доступ есть. `401 Unauthorized` — перепроверь,
что используешь именно app-specific password, а не обычный.

---

# Шаг 4. Google Calendar: OAuth

Самый длинный шаг. Суть: создаём в Google Cloud приложение, разрешаем ему
Calendar API, один раз входим в браузере и получаем `token.json`, который дальше
работает сам.

**Важно:** авторизация проходит **на Mac** (нужен браузер). На сервер потом
копируется готовый `token.json`.

## 4.1. Проект

1. [console.cloud.google.com](https://console.cloud.google.com/) → войди тем
   аккаунтом, **в чей календарь** будут писаться события.
2. Селектор проектов вверху → **New Project**.
3. Name: `tg-scheduler-bot` → **Create**. Дождись создания и **переключись на него**
   (частая ошибка — дальше настраивать в чужом проекте).

## 4.2. Включить Calendar API

**APIs & Services → Library** → найди **Google Calendar API** → **Enable**.

## 4.3. OAuth consent screen

**APIs & Services → OAuth consent screen**:

1. User type: **External** → Create.
   (*Internal* доступен только для Google Workspace-организаций.)
2. App name: `tg-scheduler-bot`; User support email и Developer contact — твоя почта.
3. **Scopes** — можно пропустить, скрипт запросит нужный сам.
4. **Test users → Add users** → добавь **свой** gmail-адрес. Без этого вход
   вернёт `Error 403: access_denied`.
5. Save.

> ⚠️ **Самая частая причина, по которой такой бот «молча ломается через неделю».**
> Пока приложение в статусе **Testing**, Google протухает refresh-токен через
> **7 дней** — бот перестанет писать в Google Calendar, и узнаешь ты об этом не
> сразу. Варианта два:
>
> - **Рекомендуемый:** на странице OAuth consent screen нажми **Publish app**
>   (статус станет *In production*). Для приложения, которое просит только
>   доступ к календарю и используется одним человеком, верификация Google не
>   требуется — просто на экране входа будет предупреждение «Google hasn't
>   verified this app», жми **Advanced → Go to tg-scheduler-bot (unsafe)**.
>   Refresh-токен после этого живёт, пока ты сам не отзовёшь доступ.
> - Оставить Testing и раз в неделю перезапускать `google_auth_setup.py`
>   и заново копировать `token.json` на сервер. Для боевого бота — плохо.

## 4.4. OAuth client → credentials.json

**APIs & Services → Credentials → Create credentials → OAuth client ID**:

1. Application type: **Desktop app** (именно Desktop — он разрешает redirect на
   `localhost`, на котором работает скрипт).
2. Name: `tg-scheduler-bot-desktop` → **Create**.
3. В окне с ключами → **Download JSON**.
4. Положи файл в каталог проекта под именем **`credentials.json`**:

```bash
mv ~/Downloads/client_secret_*.json ./credentials.json
chmod 600 credentials.json
```

## 4.5. Получить token.json

На Mac, в каталоге проекта:

```bash
python3 -m venv venv
source venv/bin/activate
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
python3 google_auth_setup.py
```

Откроется браузер → выбери аккаунт → «Google hasn't verified this app» →
**Advanced → Go to ... (unsafe)** → **Continue/Allow**.

В терминале должно появиться:

```
Сохранил token.json (права 600).

Доступ подтверждён. Календарей: 3
  - ivan@gmail.com  (id: ivan@gmail.com)  <- primary
  ...
```

Если скрипт написал «в токене нет refresh_token» — удали `token.json` и запусти
его ещё раз; без refresh-токена бот проработает около часа.

Хочешь писать не в основной календарь — впиши нужный `id` из этого списка в
`GOOGLE_CALENDAR_ID` в `.env`.

## 4.6. Что в итоге есть на Mac

```
.env                # заполнен на шагах 1–3
credentials.json    # шаг 4.4
token.json          # шаг 4.5
```

Эти три файла дальше поедут на сервер. **В git они не идут.**

---

# Шаг 5. Приватный репозиторий на GitHub

## 5.1. Через gh CLI (быстрее)

```bash
# установка и вход, если ещё нет
brew install gh
gh auth login          # GitHub.com -> HTTPS -> Login with a web browser

cd путь/к/проекту
git init -b main
git add .
git status --short     # ПРОВЕРЬ: .env / credentials.json / token.json тут быть не должно
git commit -m "ТГ-планировщик: первая версия"

gh repo create tg-scheduler-bot --private --source=. --remote=origin --push
```

## 5.2. Вручную, без gh

1. [github.com/new](https://github.com/new) → Repository name `tg-scheduler-bot`
   → **Private** → без README/gitignore → **Create**.
2. Локально:

```bash
git init -b main
git add .
git status --short     # та же проверка на секреты
git commit -m "ТГ-планировщик: первая версия"
git remote add origin git@github.com:ТВОЙ_ЛОГИН/tg-scheduler-bot.git
git push -u origin main
```

Если `git@github.com` просит пароль — SSH-ключ к GitHub не привязан; либо
привяжи его (Settings → SSH and GPG keys), либо используй HTTPS-адрес
`https://github.com/ТВОЙ_ЛОГИН/tg-scheduler-bot.git`.

---

# Шаг 6. Сервер 24/7

Аккаунт и оплату оформляешь сам — это твои паспортные и карточные данные.

## 6.1. Что выбрать

| Вариант | Цена | Ресурсы | Реальность |
| --- | --- | --- | --- |
| **Oracle Cloud Always Free** | 0 ₽ навсегда | 2× AMD micro (1 ГБ RAM) или ARM Ampere A1 (до 4 ядер / 24 ГБ) | Бесплатно, но регистрация капризная: нужна карта для верификации (списывается и возвращается ~$1), ARM-инстансы часто отдают `Out of host capacity`, а бездействующие Free-инстансы Oracle может «переработать» |
| **Hetzner Cloud** | ~4–5 €/мес | CX22: 2 vCPU / 4 ГБ / 40 ГБ | Самый предсказуемый путь. Иногда просит верификацию личности у новых аккаунтов. Оплата картой/PayPal |
| **Timeweb Cloud** | ~250–400 ₽/мес | 1 vCPU / 2 ГБ | Российский, принимает российские карты — если с зарубежной оплатой проблемы |

*Цены и лимиты приведены ориентировочно, сверь на сайте провайдера.*

Боту хватит **1 ГБ RAM** — тяжёлых вычислений нет, Whisper и GPT крутятся на
стороне OpenAI. Так что бесплатный Oracle micro справится.

**Мой совет:** попробуй Oracle; если на этапе создания инстанса упрёшься в
`Out of host capacity` или в отказ при верификации карты — не трать на это
вечер, бери Hetzner CX22 за ~4 €. Разница в деньгах меньше, чем цена возни.

ОС в любом случае: **Ubuntu 24.04 LTS**.

## 6.2. SSH-ключ (сделай до создания сервера)

На Mac:

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "tg-scheduler-bot"
cat ~/.ssh/id_ed25519.pub      # эту строку вставишь в панели провайдера
```

## 6.3. Oracle Cloud

1. [cloud.oracle.com](https://cloud.oracle.com/) → **Start for free**. Страна
   меняется только пересозданием аккаунта — выбирай внимательно.
2. Верификация картой (спишется и вернётся около $1). Апгрейда на платный
   тариф не будет, пока сам не нажмёшь.
3. После регистрации: **Compute → Instances → Create instance**.
4. **Image**: Canonical Ubuntu 24.04.
5. **Shape**: `VM.Standard.E2.1.Micro` (AMD, всегда доступен) либо
   `VM.Standard.A1.Flex` (ARM, 1 ядро / 6 ГБ — щедрее, но часто нет мест).
   Следи, чтобы стояла плашка **Always Free eligible**.
6. **Add SSH keys** → *Paste public keys* → вставь `~/.ssh/id_ed25519.pub`.
7. **Create**. Через минуту скопируй **Public IP address**.
8. **Обязательно:** Oracle по умолчанию закрывает весь трафик. Боту входящие
   порты не нужны (он сам ходит наружу), но убедись, что 22/tcp открыт —
   в **Subnet → Security List → Ingress Rules** должно быть правило на
   `0.0.0.0/0`, порт 22.

## 6.4. Hetzner

1. [console.hetzner.cloud](https://console.hetzner.cloud/) → регистрация.
2. **New project** → **Add server**.
3. Location: Nuremberg/Helsinki. Image: **Ubuntu 24.04**. Type: **CX22**.
4. **SSH keys → Add SSH key** → вставь публичный ключ.
5. **Create & Buy now** → скопируй IP.

## 6.5. Первый вход и базовая защита

```bash
ssh ubuntu@IP_СЕРВЕРА     # Oracle: пользователь ubuntu; Hetzner: root
```

На сервере:

```bash
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone Europe/Moscow   # чтобы время в логах совпадало с твоим

# вход только по ключу
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

# фаервол: боту не нужны входящие порты, кроме SSH
sudo apt -y install ufw
sudo ufw allow OpenSSH
sudo ufw --force enable

# автоматические security-обновления
sudo apt -y install unattended-upgrades
```

> Не закрывай текущую SSH-сессию, пока не проверишь вход в **новом** окне
> терминала — иначе при ошибке в конфиге останешься снаружи.

---

# Шаг 7. Развернуть бота

## 7.1. Залить код на сервер

Вариант через git (удобнее для обновлений):

```bash
# на сервере
sudo apt -y install git
git clone https://github.com/ТВОЙ_ЛОГИН/tg-scheduler-bot.git ~/tg-scheduler-bot
# для приватного репо GitHub спросит логин и Personal Access Token
# (github.com -> Settings -> Developer settings -> Tokens, scope: repo)
```

Вариант без git — просто скопировать с Mac:

```bash
# на Mac, из каталога проекта
rsync -av --exclude venv --exclude .git ./ ubuntu@IP_СЕРВЕРА:~/tg-scheduler-bot/
```

## 7.2. Перевезти секреты

Их нет в репозитории, поэтому отдельно, с Mac:

```bash
scp .env credentials.json token.json ubuntu@IP_СЕРВЕРА:~/tg-scheduler-bot/
```

## 7.3. Запустить установку

```bash
# на сервере
cd ~/tg-scheduler-bot
sudo ./deploy.sh
```

`deploy.sh` сам: проверит наличие секретов и заполненность `.env`, поставит
пакеты, заведёт системного пользователя `botuser`, скопирует код в
`/opt/tg-scheduler-bot`, создаст venv и поставит зависимости, выставит права
`600` на секреты, подставит правильные `User` и `WorkingDirectory` в
systemd-юнит, включит автозапуск и покажет первые строки лога.

Скрипт идемпотентен: `sudo ./deploy.sh` можно запускать повторно для обновления.

Хочешь другие пути:

```bash
sudo APP_USER=myuser APP_DIR=/srv/bot -E ./deploy.sh
```

---

# Шаг 8. Проверка

## 8.1. Процесс жив

```bash
systemctl status tg-scheduler-bot          # Active: active (running)
systemctl is-enabled tg-scheduler-bot      # enabled  <- переживёт перезагрузку
journalctl -u tg-scheduler-bot -n 50 --no-pager
journalctl -u tg-scheduler-bot -f          # смотреть в реальном времени
```

В логе не должно быть трассировок. Норма — что-то вроде
`Application started` и `Daily job scheduled at 08:45 Europe/Moscow`.

## 8.2. Бот отвечает

В Telegram напиши боту `/start`. Должен ответить.

Заодно проверь защиту: попроси кого-нибудь написать боту — он должен
проигнорировать чужого или ответить отказом.

## 8.3. Текстовое сообщение

Отправь боту:

```
Завтра в 15:00 созвон с командой на час, в 18:30 спортзал
```

Проверь, что появилось **в обоих** календарях:

- Google: [calendar.google.com](https://calendar.google.com/)
- iCloud: приложение «Календарь» на Mac (если не видно сразу — **Вид →
  Обновить календари**, ⌘R) либо [icloud.com/calendar](https://www.icloud.com/calendar/)

## 8.4. Голосовое сообщение

Запиши голосовое: «завтра в 11 утра зубной, вечером в семь ужин с родителями».
Ожидаемое поведение: бот распознаёт речь, показывает распознанный текст и
разобранный список, события появляются в обоих календарях.

В логе видно всю цепочку: получено voice → Whisper → GPT → создано событие.

## 8.5. Удалить тестовые события

После проверки удали созданные тестовые события в обоих календарях вручную:
в Google Calendar и в «Календаре» на Mac. Удаление в iCloud-календаре на Mac
синхронизируется автоматически.

## 8.6. Переживает перезагрузку

```bash
sudo reboot
# подожди ~30 секунд
ssh ubuntu@IP_СЕРВЕРА
systemctl status tg-scheduler-bot     # снова active (running)
```

Затем ещё раз `/start` в Telegram.

## 8.7. Ежедневный вопрос приходит

Полная проверка — дождаться 08:45 МСК. Чтобы не ждать сутки, есть два способа:

- команда `/plan` — задаёт тот же вопрос прямо сейчас;
- временно поставить в `.env` время на пару минут вперёд, перезапустить
  (`sudo systemctl restart tg-scheduler-bot`), дождаться сообщения и вернуть
  `08:45` обратно, снова перезапустив.

---

# Шаг 9. Эксплуатация

## Частые команды

```bash
systemctl status tg-scheduler-bot            # состояние
sudo systemctl restart tg-scheduler-bot      # перезапуск (после правки .env)
sudo systemctl stop tg-scheduler-bot         # остановить
journalctl -u tg-scheduler-bot -f            # логи в реальном времени
journalctl -u tg-scheduler-bot --since today # логи за сегодня
journalctl -u tg-scheduler-bot -p err        # только ошибки
```

## Поменять настройку (время вопроса, календарь, модель)

```bash
sudo -u botuser nano /opt/tg-scheduler-bot/.env
sudo systemctl restart tg-scheduler-bot
```

## Выкатить новую версию кода

```bash
cd ~/tg-scheduler-bot
git pull
sudo ./deploy.sh       # пересоберёт зависимости и перезапустит сервис
```

## Ротация ключей

| Что | Где отозвать / перевыпустить |
| --- | --- |
| Telegram token | @BotFather → `/revoke` |
| OpenAI key | platform.openai.com → API keys → Revoke |
| iCloud app password | appleid.apple.com → App-Specific Passwords → Revoke |
| Google OAuth | myaccount.google.com/permissions → убрать доступ приложения |

После любой замены: поправить `.env` на сервере и `sudo systemctl restart tg-scheduler-bot`.

## За чем следить

- **Баланс OpenAI.** Кончится — бот перестанет разбирать сообщения. Поставь
  budget alert в настройках OpenAI.
- **Google refresh-токен.** Если приложение осталось в статусе *Testing* —
  протухнет через 7 дней (см. шаг 4.3).
- **Свободное место.** `df -h`; журнал можно подрезать:
  `sudo journalctl --vacuum-time=30d`.

---

# Приложение A — как устроен код

## A.1. Структура

```
bot/
  __main__.py        точка входа: конфиг -> клиенты -> хендлеры -> поллинг
  config.py          чтение и валидация .env, единственное место с os.environ
  telegram_bot.py    хендлеры: /start, /plan, /today, текст, голос, вопрос в 08:45
  transcribe.py      голосовое (OGG/Opus) -> текст через Whisper
  parser.py          текст -> список событий через GPT, с валидацией ответа
  calendars/
    base.py          модель Event и детерминированный UID
    google.py        запись в Google Calendar
    icloud.py        запись в iCloud через CalDAV
tests/               тесты чистой логики, сеть не нужна
```

Юнит запускает `python -m bot` из рабочего каталога — оттуда же читается `.env`.

## A.2. Решения, которые стоит знать

**Конфигурация проверяется вся сразу.** `config.py` собирает список всех
проблем и падает с одним внятным сообщением, а не по одной ошибке за перезапуск:

```
Конфигурация в .env непригодна:
  - Не заданы обязательные переменные: TELEGRAM_BOT_TOKEN, OPENAI_API_KEY
  - Не найден token.json. Получи его на своей машине: python3 google_auth_setup.py
```

**Чужие сообщения не обрабатываются.** Первая строка каждого хендлера —
проверка `user.id == ALLOWED_USER_ID`; попытка постороннего пишется в лог.

**Дубли невозможны по построению.** У события есть UID, посчитанный из его
содержимого (заголовок + время). Google получает его как id события, iCloud —
как UID в VEVENT. Повторная обработка того же сообщения возвращает «уже было»,
а не создаёт вторую запись.

> Побочный эффект: Google держит id занятым и после удаления события. Если
> удалить тестовое событие и отправить тот же текст снова, бот ответит
> «уже было», хотя в календаре пусто. Сдвинь время в тексте — id станет другим.

**Частичный успех виден.** Календари пишутся параллельно и независимо; отказ
одного не отменяет запись в другой:

```
Записал 2 события:

• Созвон с командой — 16.09 15:00–16:00
  Google ✓ · iCloud ✓
• Спортзал — 16.09 18:30–19:30
  Google ✓ · iCloud ✗ (401: неверный Apple ID или пароль)
```

**Блокирующие клиенты не держат событийный цикл.** `google-api-python-client`
и `caldav` синхронные, поэтому их вызовы уходят в потоки через
`asyncio.to_thread`, а доступ к каждому клиенту сериализован замком.

**Ошибки не роняют процесс.** Сбой OpenAI или календаря превращается в
понятный ответ в Telegram и запись в лог. `Restart=always` в юните — страховка
от непредвиденного, а не штатный способ пережить ошибку.

**В логах нет секретов.** Библиотеки, умеющие печатать заголовки запросов
(`httpx`, `openai`, `telegram`, `caldav`), придавлены до `WARNING`.

## A.3. Время

Системное время на сервере — UTC, поэтому тайм-зона задаётся явно везде:
ежедневная задача получает `time(8, 45, tzinfo=ZoneInfo(TIMEZONE))`, а GPT
получает в промпте текущую дату, день недели и зону — без этого «завтра в
15:00» разбирается мимо.

В iCloud время пишется в UTC (`DTSTART:...Z`), чтобы не тащить в ICS
компонент `VTIMEZONE`; «Календарь» на Mac покажет его в локальной зоне.

## A.4. Google-авторизация

Бот не проходит OAuth сам — читает готовый `token.json` и обновляет его по
refresh-токену, сохраняя обратно на диск. Поэтому файл должен быть доступен на
запись пользователю `botuser`; `deploy.sh` права выставляет правильно.

## A.5. Тесты

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Тесты покрывают то, что можно проверить без сети: валидацию конфигурации,
разбор ответа модели (включая кривые и неполные ответы), детерминированность
UID и сборку iCalendar. Обращений к Telegram, OpenAI и календарям в них нет.

---

# Приложение B — если что-то не работает

| Симптом | Причина и что делать |
| --- | --- |
| `deploy.sh`: «Нет файла .env» | Секреты не скопированы на сервер — шаг 7.2 |
| `deploy.sh`: «Нет кода бота» | В каталоге нет `bot/` — код скопирован не полностью, повтори шаг 7.1 |
| `systemctl status` → `code=exited, status=203/EXEC` | Неверный путь в `ExecStart` или venv не создан. `ls -l /opt/tg-scheduler-bot/venv/bin/python` |
| `status=217/USER` | Пользователя из `User=` не существует |
| В логе `ModuleNotFoundError` | Зависимость не установлена: `sudo ./deploy.sh` ещё раз |
| В логе `Permission denied` на `token.json` | Права/владелец сбиты: `sudo chown botuser:botuser /opt/tg-scheduler-bot/token.json` |
| Бот молчит на `/start` | Неверный `TELEGRAM_BOT_TOKEN`, или пишешь не с того аккаунта (`ALLOWED_USER_ID`), или сервис не запущен |
| `Conflict: terminated by other getUpdates` | Тот же токен используется где-то ещё (локальная копия на Mac) — останови её |
| OpenAI `429 insufficient_quota` | Пустой баланс API — шаг 2 |
| OpenAI `401` | Ключ неверный или отозван |
| Google `invalid_grant` | Refresh-токен протух (статус *Testing*, шаг 4.3) или доступ отозван. Перезапусти `google_auth_setup.py` на Mac и скопируй новый `token.json` |
| Google `403 accessNotConfigured` | Calendar API не включён в проекте — шаг 4.2 |
| Google `Error 403: access_denied` при входе | Твой адрес не добавлен в Test users — шаг 4.3 |
| CalDAV `401 Unauthorized` | Используется обычный пароль вместо app-specific — шаг 3 |
| Бот отвечает «уже было», хотя события нет | Google держит id удалённого события занятым. Измени время в тексте — id пересчитается (см. A.2) |
| События есть в Google, но нет на Mac | Календарь создан «На моём Mac», а не в iCloud; либо в «Календаре» выключена галочка нужного календаря |
| Вопрос не приходит в 08:45 | Не установлен extra `[job-queue]` (`pip show APScheduler` на сервере); или в `.env` сбита `TIMEZONE` |
| Бот циклически перезапускается | `journalctl -u tg-scheduler-bot -n 100` — смотри трассировку. После 5 падений за 5 минут systemd остановит попытки: `sudo systemctl reset-failed tg-scheduler-bot` |

## Как собрать информацию для диагностики

```bash
systemctl status tg-scheduler-bot --no-pager
journalctl -u tg-scheduler-bot -n 100 --no-pager
ls -la /opt/tg-scheduler-bot
sudo -u botuser /opt/tg-scheduler-bot/venv/bin/pip list
```

Вывод можно присылать целиком — секретов в нём нет: логирование библиотек,
умеющих печатать заголовки запросов, придавлено до `WARNING` (см. A.2).
