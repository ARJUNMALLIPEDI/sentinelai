# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install ".[dev]" && rm -rf app

COPY . .
RUN pip install --no-deps -e .

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 sentinel
USER sentinel

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health/live', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
