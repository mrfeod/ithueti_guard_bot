FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/guard_bot.sqlite3

WORKDIR /app

RUN addgroup --system bot && adduser --system --ingroup bot bot

COPY pyproject.toml README.md ./
COPY guard_bot ./guard_bot

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

RUN chown -R bot:bot /app

USER bot

CMD ["python", "-m", "guard_bot"]
