FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN addgroup --system --gid 10001 bot \
    && adduser --system --uid 10001 --ingroup bot --home /app bot

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot schedule_bot ./schedule_bot
COPY --chown=bot:bot media ./media
COPY --chown=bot:bot config.json ./

RUN mkdir -p /data \
    && chown bot:bot /data

USER bot
VOLUME ["/data"]

CMD ["python", "-m", "schedule_bot"]
