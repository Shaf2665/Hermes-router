FROM python:3.12-slim

LABEL org.opencontainers.image.title="Hermes Router" \
      org.opencontainers.image.description="Multi-provider AI router with a built-in dashboard" \
      org.opencontainers.image.source="https://github.com/Shaf2665/Hermes-router" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Hermes Router is an inference gateway; Hall-owned coding execution is not
# bundled into this image.
COPY *.py dashboard.html ./

RUN mkdir -p /app/data
ENV HR_ENV_FILE=/app/data/.env \
    ROUTER_AUTH_FILE=/app/data/auth.json \
    ROUTER_STATE_FILE=/app/data/router_state.json \
    HERMES_INSTANCES_FILE=/app/data/instances.json \
    HR_USAGE_DB=/app/data/usage.db \
    CACHE_DB_PATH=/app/data/cache.db
VOLUME ["/app/data"]

# Port 8319 serves the API (/v1/*), /health, /metrics, and the built-in
# monitoring dashboard (open http://localhost:8319/ in a browser).
EXPOSE 8319

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8319'), timeout=4)"

CMD ["python", "router.py"]
