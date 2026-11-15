FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

# data/ (database), backups/ and logs/ are mounted from the host (see docker-compose.yml)
RUN mkdir -p data backups logs && useradd -u 1000 -m botuser && chown -R botuser /app
USER botuser

HEALTHCHECK --interval=2m --timeout=10s --start-period=3m --retries=3 \
  CMD python -m bot.healthcheck || exit 1

CMD ["python", "-m", "bot.main"]
