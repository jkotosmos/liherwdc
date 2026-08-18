# Образ для Amvera Cloud.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Слушаем все интерфейсы: снаружи запросы приходят через прокси Amvera.
    OPERON_HOST=0.0.0.0 \
    OPERON_PORT=80 \
    # Состояние держим на постоянном диске, иначе оно исчезнет при передеплое.
    OPERON_DATA_DIR=/data/state \
    OPERON_CREDENTIALS_DIR=/data/credentials \
    OPERON_KB_DIR=/data/knowledge_base

WORKDIR /app

# Зависимости — отдельным слоем: пересобираются только при изменении requirements.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x docker-entrypoint.sh

EXPOSE 80

ENTRYPOINT ["./docker-entrypoint.sh"]
