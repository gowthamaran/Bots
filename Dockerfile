FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY polymarket_lp_bot ./polymarket_lp_bot
COPY config ./config
RUN pip install --no-cache-dir .

CMD ["polymarket-lp-bot", "--config", "config/config.yaml"]
