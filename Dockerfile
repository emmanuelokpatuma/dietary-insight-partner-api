FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY partner_api ./partner_api
COPY scripts ./scripts

RUN useradd --uid 10001 --no-create-home app
USER app

# Cloud Run sets $PORT. One worker per instance; Cloud Run scales by adding instances.
CMD exec uvicorn partner_api.server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips="*"
