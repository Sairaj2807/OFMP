# syntax=docker/dockerfile:1.7
# Backend image: API (uvicorn server:app), ingest worker and CLI share it.
#   docker build -t ofmp-backend .                 # runtime image
#   docker build --target test -t ofmp-test .       # runtime + tests (CI / verification)

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENVIRONMENT=production \
    DATA_DIR=/app/data \
    TOKENS_FILE=/app/data/angel_tokens.txt \
    INSTRUMENTS_CACHE_FILE=/app/data/instruments_cache.json
WORKDIR /app

# OS security fixes released after the base image was built
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 ofmp \
    && useradd --system --uid 10001 --gid ofmp --home-dir /app --shell /usr/sbin/nologin ofmp

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini config.py server.py angel_client.py angelone_autologin.py observation_store.py \
     orderbook_engine.py replay_engine.py review_cli.py export_dataset.py ./
COPY backend ./backend
COPY research ./research
COPY static ./static

RUN mkdir -p /app/data && chown -R ofmp:ofmp /app/data
USER ofmp
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/live', timeout=2).status == 200 else 1)"

# Single process by design: the live engine lives in the API process.
# --proxy-headers: client IPs come from nginx's X-Forwarded-For (the API is only reachable via nginx).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]


FROM runtime AS test
USER root
COPY requirements-dev.txt pytest.ini pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
RUN chown -R ofmp:ofmp /app
USER ofmp
ENV ENVIRONMENT=development
CMD ["python", "-m", "pytest", "-q", "-p", "no:warnings"]
