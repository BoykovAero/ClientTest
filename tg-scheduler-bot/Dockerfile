# Образ для облачных хостингов, где деплой идёт прямо из GitHub-репозитория
# и секреты задаются переменными окружения, а не файлами на диске.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при их изменении
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Процесс не должен ходить под root
RUN useradd --system --create-home --shell /usr/sbin/nologin botuser \
    && chown -R botuser:botuser /app
USER botuser

# Конфигурация приходит переменными окружения. Файла .env в образе нет и
# быть не должно — python-dotenv просто не найдёт его и возьмёт окружение.
CMD ["python", "-m", "bot"]
