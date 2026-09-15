# tg-scheduler-bot — комплект для развёртывания

Инфраструктура для бота-планировщика: каждый день в 08:45 (Europe/Moscow) бот
спрашивает в Telegram, как пройдёт день, принимает ответ текстом или голосом
(Whisper), разбирает его через GPT и пишет события одновременно в **Google
Calendar** и в **iCloud** (он же «Календарь» на Mac).

## Статус

Здесь лежит **только обвязка деплоя**. Кода бота пока нет — требования к нему
описаны в [DEPLOY.md, приложение A](DEPLOY.md#приложение-a--контракт-с-кодом).

| Файл | Что это | Где запускается |
| --- | --- | --- |
| **[DEPLOY.md](DEPLOY.md)** | пошаговое руководство: ключи → сервер → systemd → проверка | читать первым |
| `.env.example` | шаблон всех переменных окружения | Mac |
| `google_auth_setup.py` | одноразовый OAuth в Google Calendar, выдаёт `token.json` | **Mac** (нужен браузер) |
| `requirements.txt` | python-зависимости | сервер |
| `tg-scheduler-bot.service` | шаблон systemd-юнита | сервер |
| `deploy.sh` | установка: venv → зависимости → systemd → автозапуск | **сервер**, под `sudo` |

## С чего начать

```bash
cp .env.example .env && chmod 600 .env
```

Дальше — по [DEPLOY.md](DEPLOY.md), шаги 1–4: токен у @BotFather, ключ OpenAI,
app-specific password Apple, OAuth Google. Всё это не зависит от кода бота и
делается уже сейчас.

## Секреты

`.env`, `credentials.json`, `token.json` в git **не попадают** — они
перечислены в `.gitignore`. Перед пушем:

```bash
git status --short     # этих файлов быть не должно
```
