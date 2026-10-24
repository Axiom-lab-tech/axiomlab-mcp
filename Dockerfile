FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY vidgen_sdk ./vidgen_sdk

RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /bin/bash appuser

USER appuser

ENTRYPOINT ["azimo", "mcp"]
